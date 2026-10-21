import React from 'react';
import {onlineMode} from './request';
export default function AssessmentEngine({value,onChange,disabled=false}:{value:string;onChange:(value:string)=>void;disabled?:boolean}){
 return <div className="assessment"><label>Engine đánh giá<select aria-label="Engine đánh giá" value={value} disabled={disabled} onChange={e=>onChange(e.target.value)}><option value="CURRENT">Engine hiện tại</option>{!onlineMode&&<option value="JEV">Jev — thử nghiệm</option>}</select></label>
 {value==='JEV'&&<p className="notice warn">Jev thử nghiệm: gửi nội dung công khai đã lấy và tiêu chí JD tới TypeSafe để chọn bằng chứng → đánh giá tiêu chí. Backend tính điểm. Có phí; ngân sách kiểm chứng tối đa $0.50. Không tự chấm highlights, đổi model hoặc cắt nội dung. Chất lượng cần recruiter review; confidence không phải điểm phù hợp.</p>}</div>;
}
