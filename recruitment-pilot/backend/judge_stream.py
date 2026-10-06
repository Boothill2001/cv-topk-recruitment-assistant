"""Incremental complete candidate objects, never partial or repaired JSON."""
import json,re

class CandidateStream:
    def __init__(self):
        self.text='';self.position=None;self.closed=False

    def feed(self,delta):
        self.text+=delta;items=[]
        if self.position is None:
            match=re.match(r'\s*\{\s*"items"\s*:\s*\[',self.text)
            if not match:return items
            self.position=match.end()
        while not self.closed:
            while self.position<len(self.text) and self.text[self.position] in ' \r\n\t,':self.position+=1
            if self.position>=len(self.text):break
            if self.text[self.position]==']':self.closed=True;break
            if self.text[self.position]!='{':break
            try:item,end=json.JSONDecoder().raw_decode(self.text,self.position)
            except json.JSONDecodeError:break
            if not isinstance(item,dict):break
            items.append(item);self.position=end
        return items
