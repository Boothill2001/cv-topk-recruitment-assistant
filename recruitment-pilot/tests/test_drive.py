import asyncio
import io
import zipfile
from backend.integrations import Drive,extract

def test_docx_tables_and_paragraph_text():
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z:
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Python</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>SQL</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:document>')
    text=extract(data.getvalue(),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    assert 'Python' in text and 'SQL' in text

def test_drive_paginates_and_recurses_and_recognizes_folder_id_after_rename():
    class Response:
        def __init__(self,data):self.data=data
        def json(self):return self.data
    class D(Drive):
        async def request(self,path,params=None):
            if path=='files':
                if params.get('pageToken')=='page2':return Response({'files':[{'id':'b','name':'CV2','mimeType':'text/plain'}]})
                return Response({'files':[{'id':'a','name':'CV1','mimeType':'text/plain'}],'nextPageToken':'page2'})
        async def children(self,folder):
            if folder=='1kFMAzNNHclHeFJdiTkQSwz560gJOx9kC':return [{'id':'15pdh17cz8MxI5_LR1u4r8Lz4uPXzqLxM','name':'Renamed by client','mimeType':'application/vnd.google-apps.folder'}]
            if folder=='nested':return [{'id':'c','name':'CV3','mimeType':'text/plain'}]
            return [{'id':'nested','name':'nested','mimeType':'application/vnd.google-apps.folder'}]
    d=D()
    paginated=asyncio.run(Drive.children(d,'test'))
    assert [f['id'] for f in paginated]==['a','b']
    inv=asyncio.run(d.inventory())
    assert inv[0]['group']=='candidate_public' and inv[0]['id']=='c'
