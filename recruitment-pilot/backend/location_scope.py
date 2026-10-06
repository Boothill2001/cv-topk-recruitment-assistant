"""Retrieval intent only; never a nationality or eligibility inference."""
from typing import Literal
from .errors import IntegrationError
Scope=Literal['VIETNAM','INTERNATIONAL','ANY']
PREFIXES={
 'VIETNAM':'Professionals based in Vietnam. ',
 'INTERNATIONAL':'International professionals based in countries other than Vietnam. ',
 'ANY':'Professionals in Vietnam and worldwide. ',
}
def effective_query(query,scope=None,mode='people'):
    if scope is not None and (scope not in PREFIXES or mode!='people'):
        raise IntegrationError('Phạm vi địa điểm chỉ dùng cho tìm People và phải là lựa chọn hợp lệ.')
    value=PREFIXES.get(scope,'')+query.strip()
    if not 3<=len(value)<=1500:
        raise IntegrationError('Truy vấn thực tế sau thêm địa điểm cần 3–1.500 ký tự. Rút gọn nội dung; hệ thống không tự cắt.')
    return value
