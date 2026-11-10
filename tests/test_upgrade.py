import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import rules
import optimization as opt
import execute
from ads_api import AdsClient

class UpgradeTests(unittest.TestCase):
    def test_retention_numeric_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for n in (1,2,9,10):
                p=root/f'2026-10-10 & {n}';p.mkdir();(p/'总览.md').write_text('ok')
            (root/'manual').mkdir()
            (root/'2026-10-10 & 11').mkdir() # incomplete must not be considered completed
            removed=opt.prune_outputs(root,root/'2026-10-10 & 10',2)
            self.assertEqual(set(removed),{'2026-10-10 & 1','2026-10-10 & 2'})
            self.assertTrue((root/'manual').exists())
            self.assertEqual(rules.next_run_dir(root,'2026-10-10').name,'2026-10-10 & 12')
    def test_retention_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            with self.assertRaises(ValueError):opt.prune_outputs(p,p,1)
    def test_aggregation_and_history(self):
        r={'campaignId':'123456789012345678','adGroupId':'2','searchTerm':' Rain Suit ','cost':20,'clicks':10,'sales7d':0,'purchases7d':0}
        r2={**r,'searchTerm':'rain suit','cost':5,'sales7d':50,'purchases7d':1}
        result=opt.prepare_reports({'搜索词':[r,r2],'搜索词历史':[r,r2]})['搜索词'][0]
        self.assertEqual(result['purchases7d'],1)
        self.assertEqual(result['cost'],25)
        self.assertEqual(result['campaignId'],'123456789012345678')
    def test_negative_high_spend_and_history(self):
        base,_=rules.load_baseline(rules.HERE/'baseline.yaml')
        r={'cost':35,'clicks':12,'purchases7d':0,'sales7d':0,'history_available':True,'history_orders':0,'searchTerm':'waste'}
        env=rules.make_env(r,rules.baseline_env('X',base,{}),set())
        cfg=__import__('yaml').safe_load((rules.HERE/'config.yaml').read_text(encoding='utf-8'))
        rule=cfg['规则'][0]
        self.assertTrue(rules.safe_eval(rule['条件'],env))
        self.assertEqual(len(opt.evidence_gate([(r,env)],rule)[0]),1)
        r['history_orders']=1
        self.assertEqual(len(opt.evidence_gate([(r,env)],rule)[0]),0)
        r['sales7d']=None
        self.assertEqual(len(opt.evidence_gate([(r,env)],rule)[0]),0)
    def test_product_override_and_bid(self):
        env=rules.baseline_env('P03',{'target_acos':0.3,'target_cpa':15},{'P03':{'target_cpa':8}})
        self.assertEqual(env['target_cpa'],8)
        env.update({'purchases7d':2,'clicks':25,'sales7d':80,'cpc7d':1})
        bid,why=opt.harvest_bid(env,[0.8],[0.5,1,1.5],4)
        self.assertLess(bid,1)
        self.assertIn('自身CPC',why)
        self.assertIn('Amazon推荐',why)
    def test_seasonal_acos_ceiling(self):
        from datetime import date
        all_base = {'季节期': [{'名称': 'BFCM', '开始': '2026-11-20', '结束': '2026-12-02',
                                'acos上限增量': 0.4, '产品码': ['P03']}]}
        inside = rules.baseline_env('P03', {'target_acos': 0.3}, all_base, today=date(2026, 11, 25))
        self.assertAlmostEqual(inside['target_acos'], 0.7)
        outside_date = rules.baseline_env('P03', {'target_acos': 0.3}, all_base, today=date(2026, 12, 10))
        self.assertAlmostEqual(outside_date['target_acos'], 0.3)
        other_code = rules.baseline_env('P05', {'target_acos': 0.3}, all_base, today=date(2026, 11, 25))
        self.assertAlmostEqual(other_code['target_acos'], 0.3)
    def test_shared_negative_candidates(self):
        ledger_success = {
            '1': {'row': {'动作': '否定', '词': 'waste term', '匹配': 'NEGATIVE_EXACT',
                           'campaignId': '100', '来源': 'P03-AUTO-1'}},
            '2': {'row': {'动作': '否定', '词': 'waste term', '匹配': 'NEGATIVE_EXACT',
                           'campaignId': '101', '来源': 'P05-AUTO-1'}},
            '3': {'row': {'动作': '否定', '词': 'other term', '匹配': 'NEGATIVE_EXACT',
                           'campaignId': '100', '来源': 'P03-AUTO-1'}},
        }
        product_line_map = {'P03': 'LINE_A', 'P05': 'LINE_A', 'P07': 'LINE_A'}
        active_campaigns = {
            '100': {'campaignId': '100', 'name': 'P03-AUTO-1', 'state': 'ENABLED'},
            '101': {'campaignId': '101', 'name': 'P05-AUTO-1', 'state': 'ENABLED'},
            '102': {'campaignId': '102', 'name': 'P07-AUTO-1', 'state': 'ENABLED'},
        }
        out = rules.detect_shared_negative_candidates(ledger_success, product_line_map, active_campaigns)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['词'], 'waste term')
        self.assertEqual(out[0]['已否定活动数'], 2)
        self.assertEqual(out[0]['待补齐活动'], ['P07-AUTO-1'])
    def test_auto_default_bid(self):
        row={'campaignId':'1','adGroupId':'2','keywordId':'3','keywordType':'TARGETING_EXPRESSION','targeting':'close-match','cost':30,'clicks':20,'sales7d':0,'purchases7d':0}
        env=rules.make_env(row,{'bid_min':0.2,'bid_max':20},set())
        actions,skipped=rules.expand_bid([(row,env)],{'方向':'减价','比例':0.2},{'3':{'state':'ENABLED','_effective_bid':1}}, {},{},set())
        self.assertFalse(skipped)
        self.assertEqual(actions[0]['新Bid'],0.8)
    def test_target_api_shape(self):
        client=object.__new__(AdsClient)
        class Resp:
            status_code=207
            def json(self):return {'targetingClauses':{'success':[{'index':0,'targetId':'3'}],'error':[]}}
        calls=[]
        client.request=lambda *args: (calls.append(args) or Resp())
        ok,err=client.update_targets([{'targetId':'3','bid':0.8}])
        self.assertEqual(calls[0][2],{'targetingClauses':[{'targetId':'3','bid':0.8}]})
        self.assertTrue(ok);self.assertFalse(err)
    def test_missing_response_not_success(self):
        class Resp:
            text='{}'
            def json(self):return {}
        ok,err=AdsClient._parse_batch_response(Resp(),'keywords',1)
        self.assertFalse(ok);self.assertTrue(err)
    def test_execution_bid_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'data').mkdir();(root/'output').mkdir()
            for name in ('config.yaml','baseline.yaml'):(root/name).write_bytes((rules.HERE/name).read_bytes())
            csvpath=root/'output'/'01-收割.csv'
            row={'批准':'是','动作ID':'test','动作':'收割','词':'rain suit','匹配':'BROAD','campaignId':'999','adGroupId':'2','新Bid':'0.6','产品码':'X'}
            with csvpath.open('w',newline='',encoding='utf-8-sig') as f:
                w=csv.DictWriter(f,fieldnames=list(row));w.writeheader();w.writerow(row)
            class Client:
                def list_campaigns(self,*args):return [{'campaignId':'999','state':'ENABLED'}]
                def list_ad_groups(self,*args):return [{'adGroupId':'2','campaignId':'999','state':'ENABLED'}]
                def list_keywords(self,*args):return []
                def create_broad_keywords(self,items):
                    self.items=items
                    return [{'index':0,'keywordId':'123'}],[]
            c=Client()
            with patch.object(execute,'HERE',root),patch.object(opt,'HERE',root),patch.object(execute.time,'sleep'):
                self.assertTrue(execute.execute_file(c,csvpath,False,200))
                self.assertFalse(hasattr(c,'items'))
                self.assertTrue(execute.execute_file(c,csvpath,True,200))
            self.assertEqual(c.items[0]['bid'],0.6)
            self.assertEqual(opt.read_json(root/'data'/'keywords_state.json',[])[0]['keywordId'],'123')
            self.assertFalse(opt.read_json(root/'data'/'execution_ledger.json',{})['pending'])
    def test_ledger_syncs_to_db(self):
        """Phase 3: execute.py 执行结果应该镜像进 action_ledger 表，三种结果分别对应
        SUCCESS / PENDING（结果不确定，未弹出）/ FAILED（确认失败，从 pending 弹出）。"""
        import sqlite3
        def run_one(row, client, tmp_name):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'data').mkdir();(root/'output').mkdir()
                for name in ('config.yaml','baseline.yaml'):
                    (root/name).write_bytes((rules.HERE/name).read_bytes())
                csvpath=root/'output'/tmp_name
                with csvpath.open('w',newline='',encoding='utf-8-sig') as f:
                    w=csv.DictWriter(f,fieldnames=list(row));w.writeheader();w.writerow(row)
                with patch.object(execute,'HERE',root),patch.object(opt,'HERE',root),patch.object(execute.time,'sleep'):
                    execute.execute_file(client,csvpath,True,200)
                conn=sqlite3.connect(str(root/'data'/'ads.db'));conn.row_factory=sqlite3.Row
                r=conn.execute("SELECT status FROM action_ledger WHERE action_id=?",(row['动作ID'],)).fetchone()
                conn.close()
                return r['status'] if r else None

        harvest_row={'批准':'是','动作ID':'t-ok','动作':'收割','词':'rain suit','匹配':'BROAD',
                    'campaignId':'999','adGroupId':'2','新Bid':'0.6','产品码':'X'}
        class OkClient:
            def list_campaigns(self,*a):return [{'campaignId':'999','state':'ENABLED'}]
            def list_ad_groups(self,*a):return [{'adGroupId':'2','campaignId':'999','state':'ENABLED'}]
            def list_keywords(self,*a):return []
            def create_broad_keywords(self,items):return [{'index':0,'keywordId':'123'}],[]
        self.assertEqual(run_one(harvest_row,OkClient(),'01-收割.csv'),'SUCCESS')

        neg_row={'批准':'是','动作ID':'t-pending','动作':'否定','词':'waste term','匹配':'NEGATIVE_EXACT',
                 'campaignId':'999','adGroupId':'2','产品码':'X'}
        class UnconfirmedClient:
            def list_campaigns(self,*a):return [{'campaignId':'999','state':'ENABLED'}]
            def list_ad_groups(self,*a):return [{'adGroupId':'2','campaignId':'999','state':'ENABLED'}]
            def list_negative_keywords(self,*a):return []
            def create_negative_exact(self,items):return [],[{'index':0,'code':'UNCONFIRMED','detail':'timeout'}]
        self.assertEqual(run_one(neg_row,UnconfirmedClient(),'04-否定.csv'),'PENDING')

        budget_row={'批准':'是','动作ID':'t-failed','动作':'预算','campaignId':'999','原预算':'40',
                    '新预算':'45','产品码':'X','来源':'test-campaign'}
        class FailClient:
            def list_campaigns(self,*a):return [{'campaignId':'999','state':'ENABLED','budget':{'budget':40}}]
            def list_ad_groups(self,*a):return [{'adGroupId':'2','campaignId':'999','state':'ENABLED'}]
            def update_campaigns(self,items):return [],[{'index':0,'code':'VALIDATION_ERROR','detail':'bad budget'}]
        self.assertEqual(run_one(budget_row,FailClient(),'02-加预算.csv'),'FAILED')
    def test_filtered_error_only_400_fallback(self):
        calls=[]
        def fn(filters=None):
            calls.append(filters)
            if filters:raise RuntimeError('HTTP 400 invalid filter')
            return [1]
        self.assertEqual(opt.filtered_list(fn,{'stateFilter':{}}),[1])
        self.assertEqual(len(calls),2)
        with self.assertRaises(RuntimeError):
            opt.filtered_list(lambda f: (_ for _ in ()).throw(RuntimeError('HTTP 403')),{'x':1})

    def test_generate_three_runs_retains_two(self):
        import copy
        import yaml
        import ads_api
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'data').mkdir();(root/'output').mkdir()
            for name in ('config.yaml','baseline.yaml','保护词.txt'):
                (root/name).write_bytes((rules.HERE/name).read_bytes())
            opt.write_json(root/'data'/'auto_broad_map.json',{'map':{'111':{'broad':[{'cid':'999','state':'ENABLED'}],'adGroupIds':['2']}}})
            cs=[{'campaignId':'111','state':'ENABLED','targetingType':'AUTO'}, {'campaignId':'999','state':'ENABLED','targetingType':'MANUAL'}]
            gs=[{'campaignId':'111','adGroupId':'1','state':'ENABLED','defaultBid':1},{'campaignId':'999','adGroupId':'2','state':'ENABLED','defaultBid':2}]
            ts=[{'campaignId':'111','adGroupId':'1','targetId':'3','state':'ENABLED','bid':1}]
            def snapshots(*args):
                for name,rows in (('campaigns',cs),('adgroups',gs),('targets',ts)):
                    opt.write_json(root/'data'/f'{name}_all.json',rows)
            common={'campaignId':'111','campaignName':'P03-AUTO','adGroupId':'1','impressions':100,'_report_days':7}
            reports={'搜索词':[{**common,'searchTerm':'rain suit','cost':10,'clicks':20,'sales7d':100,'purchases7d':2},
                              {**common,'searchTerm':'waste term','cost':35,'clicks':12,'sales7d':0,'purchases7d':0}],
                     '广告活动':[{**common,'campaignStatus':'ENABLED','campaignBudgetAmount':10,'cost':2,'clicks':10,'sales7d':100,'purchases7d':2}],
                     'AUTO投放':[{**common,'keywordId':'3','keywordType':'TARGETING_EXPRESSION','targeting':'close-match','cost':35,'clicks':12,'sales7d':0,'purchases7d':0}]}
            for name in list(reports):
                reports[name+'历史']=copy.deepcopy(reports[name])
            class Client:
                def recommend_bids(self,*args):return {('KEYWORD_BROAD_MATCH','rain suit'):[0.5,0.7,0.9]}
            with patch.object(rules,'HERE',root),patch.object(opt,'HERE',root), \
                 patch.object(rules,'CAMPAIGN_SNAPSHOT',root/'data'/'campaigns_all.json'), \
                 patch.object(rules,'ADGROUP_SNAPSHOT',root/'data'/'adgroups_all.json'), \
                 patch.object(rules,'TARGET_SNAPSHOT',root/'data'/'targets_all.json'), \
                 patch.object(rules,'refresh_pool',snapshots),patch.object(rules,'sync_keywords',return_value=[]), \
                 patch.object(rules,'pull_reports',side_effect=lambda *a,**kw:copy.deepcopy(reports)), \
                 patch.object(ads_api,'AdsClient',return_value=Client()):
                for _ in range(3):rules.suggest()
            runs=sorted(p for p in (root/'output').iterdir() if p.is_dir())
            self.assertEqual(len(runs),2)
            self.assertTrue(runs[-1].name.endswith('& 3'))
            with (runs[-1]/'01-收割.csv').open(encoding='utf-8-sig') as f:
                harvest=list(csv.DictReader(f))
            self.assertGreater(float(harvest[0]['新Bid']),0)
            with (runs[-1]/'04-否定.csv').open(encoding='utf-8-sig') as f:
                negative=list(csv.DictReader(f))
            self.assertEqual(negative[0]['批准'],'否')
            self.assertTrue((runs[-1]/'05-BID调整.csv').exists())

    def test_only_board_runs_and_pulls_only_that_board(self):
        """--only 收割: 跑收割+收割ASIN规则 (并入同一板块), 只出 01-收割.csv。"""
        import copy
        import ads_api
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'data').mkdir();(root/'output').mkdir()
            for name in ('config.yaml','baseline.yaml','保护词.txt'):
                (root/name).write_bytes((rules.HERE/name).read_bytes())
            opt.write_json(root/'data'/'auto_broad_map.json',{'map':{'111':{'broad':[{'cid':'999','state':'ENABLED'}],'adGroupIds':['2']}}})
            cs=[{'campaignId':'111','state':'ENABLED','targetingType':'AUTO'},{'campaignId':'999','state':'ENABLED','targetingType':'MANUAL'}]
            gs=[{'campaignId':'111','adGroupId':'1','state':'ENABLED','defaultBid':1},{'campaignId':'999','adGroupId':'2','state':'ENABLED','defaultBid':2}]
            ts=[{'campaignId':'111','adGroupId':'1','targetId':'3','state':'ENABLED','bid':1}]
            def snapshots(*args):
                for name,rows in (('campaigns',cs),('adgroups',gs),('targets',ts)):
                    opt.write_json(root/'data'/f'{name}_all.json',rows)
            common={'campaignId':'111','campaignName':'P03-AUTO','adGroupId':'1','impressions':100,'_report_days':7}
            # 两个板块都有数据: 搜索词能出收割+否定, AUTO投放能出 BID调整
            reports={'搜索词':[{**common,'searchTerm':'rain suit','cost':10,'clicks':20,'sales7d':100,'purchases7d':2},
                              {**common,'searchTerm':'waste term','cost':35,'clicks':12,'sales7d':0,'purchases7d':0}],
                     'AUTO投放':[{**common,'keywordId':'3','keywordType':'TARGETING_EXPRESSION','targeting':'close-match','cost':35,'clicks':12,'sales7d':0,'purchases7d':0}]}
            for name in list(reports):
                reports[name+'历史']=copy.deepcopy(reports[name])
            asked=[]
            def fake_pull(client,specs,polling=None,profile_id=None,conn=None):
                asked.extend(s['名称'] for s in specs)
                return {s['名称']:copy.deepcopy(reports.get(s['名称'],[])) for s in specs}
            class Client:
                def recommend_bids(self,*args):return {('KEYWORD_BROAD_MATCH','rain suit'):[0.5,0.7,0.9]}
            with patch.object(rules,'HERE',root),patch.object(opt,'HERE',root), \
                 patch.object(rules,'CAMPAIGN_SNAPSHOT',root/'data'/'campaigns_all.json'), \
                 patch.object(rules,'ADGROUP_SNAPSHOT',root/'data'/'adgroups_all.json'), \
                 patch.object(rules,'TARGET_SNAPSHOT',root/'data'/'targets_all.json'), \
                 patch.object(rules,'refresh_pool',snapshots),patch.object(rules,'sync_keywords',return_value=[]), \
                 patch.object(rules,'pull_reports',fake_pull), \
                 patch.object(ads_api,'AdsClient',return_value=Client()):
                self.assertEqual(rules.suggest(only='收割'),0)
            # 只拉了搜索词 + AUTO投放那几份 (收割板块现在包含 收割ASIN 规则, 数据来源是
            # AUTO投放——见 REG2/select() 的 收割ASIN 并入逻辑), 广告活动/位置表现一份都没要
            self.assertEqual(set(asked),{'搜索词','搜索词历史','AUTO投放','AUTO投放历史'})
            run_dir=sorted(p for p in (root/'output').iterdir() if p.is_dir())[-1]
            csvs={p.name for p in run_dir.glob('*.csv')}
            self.assertEqual(csvs,{'01-收割.csv'})
            self.assertEqual(json.loads((run_dir/'run_complete.json').read_text(encoding='utf-8'))['boards'],['收割'])
            self.assertIn('只出板块',(run_dir/'总览.md').read_text(encoding='utf-8'))

    def test_only_board_rejects_unknown_name(self):
        with self.assertRaises(SystemExit):
            rules.resolve_boards('乱写的板块')
        self.assertEqual(rules.resolve_boards(None),rules.BOARD_ORDER)
        self.assertEqual(rules.resolve_boards('BID'),['BID调整'])
        self.assertEqual(rules.resolve_boards('预算'),['加预算','减预算'])

if __name__=='__main__':unittest.main()
