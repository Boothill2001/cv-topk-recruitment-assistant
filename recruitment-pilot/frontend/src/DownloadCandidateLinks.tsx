import React from 'react';

type Source = {title?:string;url:string};
export default function DownloadCandidateLinks({results,limit,jobId,runId,provisional=false}:{results:Source[];limit:number;jobId:string;runId:string;provisional?:boolean}) {
 const visible=results.slice(0,limit);
 const download=()=>{
  const lines=[`Link ứng viên — ${jobId}`,`Mã lượt: ${runId}`,`Top ${visible.length} theo thứ hạng đang hiển thị`,...(provisional?['Kết quả tạm thời — thứ hạng có thể thay đổi khi đánh giá tiếp.']:[]),'Nguồn hồ sơ công khai; cần xác minh với ứng viên.','',...visible.flatMap((r,i)=>[`${i+1}. ${(r.title||'Hồ sơ ứng viên').replace(/[\r\n]+/g,' ')}`,r.url,''])];
  const url=URL.createObjectURL(new Blob(['\uFEFF'+lines.join('\r\n')],{type:'text/plain;charset=utf-8'}));
  const link=document.createElement('a');
  link.href=url;link.download=`Link_ung_vien_${jobId}_${runId}_Top${visible.length}.txt`.replace(/[<>:"/\\|?*]/g,'_');
  document.body.appendChild(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
 };
 return <button className="secondary" disabled={!visible.length} onClick={download}>Tải link ứng viên (.txt) · Top {visible.length}</button>;
}
