export class RequestError extends Error {
 constructor(message:string,public status:number){super(message);this.name='RequestError'}
}
export async function request<T>(path:string,method='GET',body?:unknown):Promise<T>{
 const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),path==='/connections/google'?300000:method==='GET'?20000:90000);
 try{
  const multipart=body instanceof FormData;
  const response=await fetch('/api'+path,{method,headers:{'X-Pilot-Request':'1',...(multipart?{}:{'Content-Type':'application/json'})},signal:controller.signal,body:body===undefined?undefined:multipart?body:JSON.stringify(body)});
  let data:any;try{data=await response.json()}catch{throw new Error('Dịch vụ trả về dữ liệu không đọc được. Thử tải lại; nếu vẫn lỗi, xem trạng thái dịch vụ.');}
  if(!response.ok){const detail=data?.detail;const message=typeof detail==='string'?detail:Array.isArray(detail)?detail.map((x:any)=>((x.loc||[]).filter((v:any)=>v!=='body').join('.')+': '+x.msg)).join('; '):'Không thực hiện được thao tác ('+response.status+').';throw new RequestError(message,response.status)}
  return data;
 }catch(error){if(error instanceof DOMException&&error.name==='AbortError')throw new Error('Dịch vụ phản hồi quá lâu. Tải lại trạng thái trước khi thử lại để tránh tạo thao tác trùng.');if(error instanceof TypeError)throw new Error('Không kết nối được dịch vụ trên máy. Kiểm tra dịch vụ pilot đã mở, rồi bấm Thử tải lại.');throw error;}finally{clearTimeout(timeout)}
}
