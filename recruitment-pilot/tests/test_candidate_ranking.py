from backend.candidate_ranking import canonical_url, ranked_candidates

def test_linkedin_aliases_only():
    assert canonical_url('https://vn.linkedin.com/in/Person/?trk=test') == canonical_url('https://www.linkedin.com/in/person')
    assert canonical_url('https://fake-linkedin.com/in/person') != canonical_url('https://linkedin.com/in/person')

def test_groups_identity_and_immutable_scores():
    criteria=[{'id':'C1','type':'MUST','enabled':True}]
    def row(url, state, score, rank):
        return {'url':url,'title':'Same name','candidate_id':str(rank),'be_rank':rank,'score':score,'coverage':100,
                'assessments':[{'criterion_id':'C1','status':state}]}
    original=[row('https://linkedin.com/in/a','NOT_MET',99,1),
              row('https://linkedin.com/in/b','MET',20,2),
              row('https://vn.linkedin.com/in/b/','MET',90,3),
              row('https://linkedin.com/in/c','UNKNOWN',70,4)]
    result=ranked_candidates(original,criteria)
    assert [r['review_group'] for r in result]==['MUST_MET','MUST_UNCLEAR','MUST_NOT_MET']
    assert result[0]['score']==20
    assert len(result[0]['source_urls'])==2
    assert len(original)==4 and original[2]['score']==90
