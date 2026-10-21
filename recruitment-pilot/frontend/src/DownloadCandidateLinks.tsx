import {jobLabel} from './jobId';
import React from 'react';

type Source = {title?:string;url:string};
export default function DownloadCandidateLinks({results,limit,jobId,runId,provisional=false,ranked=true}:{results:Source[];limit:number;jobId:string;runId:string;provisional?:boolean;ranked?:boolean}) {
 const visible=results.slice(0,limit);
 const download=()=>{
  const lines=[`Link ứng viên — ${jobLabel(jobId)}`,`Mã lượt: ${runId}`,ranked?`Top ${visible.length} theo thứ hạng đang hiển thị`:`${visible.length} nguồn theo thứ tự tìm thấy — chưa chấm phù hợp JD`,...(provisional?['Kết quả tạm thời — thứ hạng có thể thay đổi khi đánh giá tiếp.']:[]),'Nguồn hồ sơ công khai; cần xác minh với ứng viên.','',...visible.flatMap((r,i)=>[`${i+1}. ${(r.title||'Hồ sơ ứng viên').replace(/[\r\n]+/g,' ')}`,r.url,''])];
  const url=URL.createObjectURL(new Blob(['\uFEFF'+lines.join('\r\n')],{type:'text/plain;charset=utf-8'}));
  const link=document.createElement('a');
  link.href=url;link.download=`Link_ung_vien_${jobLabel(jobId)}_${runId}_${ranked?'Top':'Nguon'}${visible.length}.txt`.replace(/[<>:"/\\|?*]/g,'_');
  document.body.appendChild(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
 };
 return <button className="secondary" disabled={!visible.length} onClick={download}>Tải link ứng viên (.txt) · {ranked?'Top':'Nguồn'} {visible.length}</button>;
}
