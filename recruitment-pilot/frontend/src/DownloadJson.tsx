import React,{useState} from 'react';
import {request} from './request';

export default function DownloadJson({path,filename,children}:{path:string;filename:string;children:React.ReactNode}){
 const [busy,setBusy]=useState(false),[error,setError]=useState('');
 const download=async()=>{
  setBusy(true);setError('');
  try{
   const data=await request<unknown>(path);
   const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json;charset=utf-8'}));
   const link=document.createElement('a');link.href=url;link.download=filename.replace(/[<>:"/\\|?*]/g,'_');
   document.body.appendChild(link);link.click();link.remove();
   setTimeout(()=>URL.revokeObjectURL(url),60000);
  }catch(e){setError((e as Error).message)}finally{setBusy(false)}
 };
 return <><button className="secondary" disabled={busy} onClick={download}>{busy?'Đang chuẩn bị file…':children}</button>{error&&<p role="alert" className="notice error">{error}</p>}</>;
}
