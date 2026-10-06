import React from 'react';
export type LocationScope='VIETNAM'|'INTERNATIONAL'|'ANY';
export const labels:Record<LocationScope,string>={VIETNAM:'Tại Việt Nam',INTERNATIONAL:'Quốc tế — ngoài Việt Nam',ANY:'Cả Việt Nam và quốc tế'};
const prefixes:Record<LocationScope,string>={VIETNAM:'Professionals based in Vietnam. ',INTERNATIONAL:'International professionals based in countries other than Vietnam. ',ANY:'Professionals in Vietnam and worldwide. '};
export const scopeLabel=(value?:LocationScope|null)=>value?labels[value]:'Chưa lưu phạm vi';
export const effectiveQuery=(query:string,scope:LocationScope|null)=>query.trim()?(scope?prefixes[scope]+query.trim():query.trim()):'';
export default function LocationSelector({value,onChange,disabled=false}:{value:LocationScope|null;onChange:(scope:LocationScope)=>void;disabled?:boolean}){
 return <div className="assessment"><label>Bạn muốn tìm ứng viên ở đâu?<select aria-label="Địa điểm ứng viên" value={value||''} disabled={disabled} onChange={e=>onChange(e.target.value as LocationScope)}>{!value&&<option value="" disabled>Chưa lưu phạm vi — chọn cho lượt mới</option>}{Object.entries(labels).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label><p className="muted">Kiểm tra giới hạn quốc gia và múi giờ trong JD. Phạm vi giúp tìm nguồn; địa điểm từng hồ sơ vẫn cần xác minh. Không dùng tên hoặc ngôn ngữ hồ sơ để đoán quốc tịch.</p></div>;
}
