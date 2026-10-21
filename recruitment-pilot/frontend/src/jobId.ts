export function jobLabel(id:string|null|undefined){return (id||'').replace(/^IT-([0-9]+[a-z]?)$/i,'JD$1')}
