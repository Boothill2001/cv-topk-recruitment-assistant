export type SearchType = 'auto' | 'deep';
export const searchTypeLabel = (value?:string) => value === 'deep' ? 'Deep' : 'Auto';
export default function SearchTypeSelector({value,onChange,disabled}:{value:SearchType;onChange:(value:SearchType)=>void;disabled?:boolean}){
 return <div className="assessment"><label>Chế độ tìm kiếm<select aria-label="Chế độ tìm kiếm Auto hoặc Deep" value={value} disabled={disabled} onChange={e=>onChange(e.target.value as SearchType)}><option value="auto">Auto — mặc định</option><option value="deep">Deep — tìm chuyên sâu</option></select></label><p className="muted">Auto cân bằng tốc độ và chất lượng. Deep có thể mất nhiều thời gian và phí hơn; không đảm bảo lấy đầy đủ LinkedIn. Chế độ chỉ áp dụng cho lượt tìm mới.</p></div>;
}
