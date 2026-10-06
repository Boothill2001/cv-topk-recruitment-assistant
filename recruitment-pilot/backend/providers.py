"""Provider adapters. Recruitment logic depends only on JSON + normalized usage."""
import json
import re
from .errors import IntegrationError


def text_json(text):
    # Providers sometimes wrap otherwise valid JSON in a markdown fence.
    return re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip()).strip()

def metered_error(message,usage):
    error=IntegrationError(message);error.usage=usage;return error


class DeepSeek:
    label='DeepSeek';key_env='DEEPSEEK_API_KEY'
    async def generate_stream(self,c,key,model,system,user,schema,max_tokens,on_delta):
        content='';usage={};finish=None;done=False
        try:
            async with c.stream('POST','https://api.deepseek.com/chat/completions',headers={'Authorization':'Bearer '+key},json={
                'model':model,'messages':[{'role':'system','content':system},{'role':'user','content':user}],
                'response_format':{'type':'json_object'},'thinking':{'type':'disabled'},'temperature':0,
                'max_tokens':max_tokens,'stream':True,'stream_options':{'include_usage':True}}) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith('data:'):continue
                    value=line[5:].strip()
                    if value=='[DONE]':done=True;break
                    if not value:continue
                    d=json.loads(value)
                    if d.get('error'):raise metered_error('Provider báo lỗi trong stream.',usage)
                    if d.get('usage'):usage=d['usage']
                    choices=d.get('choices',[])
                    if choices:
                        finish=choices[0].get('finish_reason') or finish
                        delta=choices[0].get('delta',{}).get('content') or ''
                        if delta:content+=delta;on_delta(delta)
        except Exception as e:
            e.usage=usage;raise
        if not done or finish!='stop' or not content:
            raise metered_error('Stream chưa hoàn chỉnh; giữ kết quả tạm riêng và thử lại theo giới hạn.',usage)
        return text_json(content),usage
    async def models(self,c,key):
        r=await c.get('https://api.deepseek.com/models',headers={'Authorization':'Bearer '+key})
        r.raise_for_status();return [m['id'] for m in r.json()['data']]
    async def generate(self,c,key,model,system,user,schema,max_tokens):
        r=await c.post('https://api.deepseek.com/chat/completions',headers={'Authorization':'Bearer '+key},json={
            'model':model,'messages':[{'role':'system','content':system},{'role':'user','content':user}],
            'response_format':{'type':'json_object'},'thinking':{'type':'disabled'},'temperature':0,'max_tokens':max_tokens})
        r.raise_for_status();d=r.json()
        choice=d['choices'][0];usage=d.get('usage',{})
        if choice.get('finish_reason') in ('length','content_filter'):
            raise metered_error('DeepSeek không trả output hoàn chỉnh; kiểm tra token limit/refusal.',usage)
        return text_json(choice['message']['content']),usage


class OpenAI:
    label='OpenAI · GPT';key_env='OPENAI_API_KEY'
    async def models(self,c,key):
        r=await c.get('https://api.openai.com/v1/models',headers={'Authorization':'Bearer '+key})
        r.raise_for_status()
        return sorted(m['id'] for m in r.json()['data'] if m['id'].startswith(('gpt-','o1','o3','o4')))
    async def generate(self,c,key,model,system,user,schema,max_tokens):
        r=await c.post('https://api.openai.com/v1/responses',headers={'Authorization':'Bearer '+key},json={
            'model':model,'instructions':system,'input':user,'store':False,
            'text':{'format':{'type':'json_object'}},'max_output_tokens':max_tokens})
        r.raise_for_status();d=r.json();u=d.get('usage',{})
        text=''.join(p.get('text','') for item in d.get('output',[]) if item.get('type')=='message'
                     for p in item.get('content',[]) if p.get('type')=='output_text')
        usage={'prompt_tokens':u.get('input_tokens',0),'completion_tokens':u.get('output_tokens',0),
               'total_tokens':u.get('total_tokens',u.get('input_tokens',0)+u.get('output_tokens',0)),'raw':u}
        if d.get('status')!='completed' or not text:
            raise metered_error('OpenAI không trả JSON hoàn chỉnh; kiểm tra model/output token limit hoặc refusal.',usage)
        return text_json(text),usage


class Anthropic:
    label='Anthropic · Claude';key_env='ANTHROPIC_API_KEY'
    def headers(self,key):return {'x-api-key':key,'anthropic-version':'2023-06-01'}
    async def models(self,c,key):
        models=[];after=None
        while True:
            r=await c.get('https://api.anthropic.com/v1/models',headers=self.headers(key),
                          params={'limit':100,**({'after_id':after} if after else {})})
            r.raise_for_status();d=r.json();models.extend(m['id'] for m in d['data'])
            if not d.get('has_more'):return models
            after=d['last_id']
    async def generate(self,c,key,model,system,user,schema,max_tokens):
        # Newer Claude models reject forced tool choice; request the data tool explicitly.
        r=await c.post('https://api.anthropic.com/v1/messages',headers=self.headers(key),json={
            'model':model,'system':system+'\nGọi emit_result đúng một lần để trả JSON.',
            'messages':[{'role':'user','content':user}],'max_tokens':max_tokens,
            'tools':[{'name':'emit_result','description':'Return the recruitment result as JSON data.', 'input_schema':schema}],
            'tool_choice':{'type':'auto'}})
        r.raise_for_status();d=r.json();u=d.get('usage',{})
        inp=sum(u.get(k,0) for k in ('input_tokens','cache_creation_input_tokens','cache_read_input_tokens'))
        usage={'prompt_tokens':inp,'completion_tokens':u.get('output_tokens',0),
               'total_tokens':inp+u.get('output_tokens',0),'raw':u}
        blocks=[p for p in d.get('content',[]) if p.get('type')=='tool_use' and p.get('name')=='emit_result']
        if d.get('stop_reason')=='max_tokens' or len(blocks)!=1:
            raise metered_error('Claude không trả structured result hoàn chỉnh; kiểm tra model hoặc token limit.',usage)
        return json.dumps(blocks[0]['input'],ensure_ascii=False),usage


class Gemini:
    label='Google · Gemini';key_env='GEMINI_API_KEY'
    async def models(self,c,key):
        models=[];token=None
        while True:
            r=await c.get('https://generativelanguage.googleapis.com/v1beta/models',headers={'x-goog-api-key':key},
                          params={'pageSize':100,**({'pageToken':token} if token else {})})
            r.raise_for_status();d=r.json()
            models.extend(m['name'].removeprefix('models/') for m in d.get('models',[])
                          if 'generateContent' in m.get('supportedGenerationMethods',[]))
            token=d.get('nextPageToken')
            if not token:return models
    async def generate(self,c,key,model,system,user,schema,max_tokens):
        if not re.fullmatch(r'[\w.\-]+',model):raise IntegrationError('Gemini model ID không hợp lệ.')
        r=await c.post('https://generativelanguage.googleapis.com/v1beta/models/'+model+':generateContent',
                      headers={'x-goog-api-key':key},json={
            'systemInstruction':{'parts':[{'text':system}]},'contents':[{'role':'user','parts':[{'text':user}]}],
            'generationConfig':{'responseMimeType':'application/json','maxOutputTokens':max_tokens}})
        r.raise_for_status();d=r.json();u=d.get('usageMetadata',{})
        usage={'prompt_tokens':u.get('promptTokenCount',0),
               'completion_tokens':u.get('candidatesTokenCount',0)+u.get('thoughtsTokenCount',0),
               'total_tokens':u.get('totalTokenCount',0),'raw':u}
        candidates=d.get('candidates',[])
        if len(candidates)!=1 or candidates[0].get('finishReason')!='STOP':
            raise metered_error('Gemini không trả JSON hoàn chỉnh; kiểm tra safety/refusal hoặc token limit.',usage)
        text=''.join(p.get('text','') for p in candidates[0].get('content',{}).get('parts',[]) if not p.get('thought'))
        return text_json(text),usage


PROVIDERS={'deepseek':DeepSeek(),'openai':OpenAI(),'anthropic':Anthropic(),'gemini':Gemini()}

def adapter(provider):
    if provider not in PROVIDERS:raise IntegrationError('Provider chưa được hỗ trợ.')
    return PROVIDERS[provider]
