from src.collectors import ecos

class R:
    stat_code='X'; cycle='D'; item_code1='A'; item_code2=None; item_code3=None

def test_fetch_paginates(monkeypatch):
    calls=[]
    def fake_page(key,resolution,start,end,start_row,end_row,timeout,retries):
        calls.append((start_row,end_row))
        if start_row == 1:
            return [{'TIME':str(i),'DATA_VALUE':'1'} for i in range(1000)]
        if start_row == 1001:
            return [{'TIME':'1001','DATA_VALUE':'2'}]
        return []
    monkeypatch.setattr(ecos,'_fetch_page',fake_page)
    rows=ecos._fetch('k',R(),'20200101','20260101',10,1)
    assert len(rows)==1001
    assert calls==[(1,1000),(1001,2000)]
