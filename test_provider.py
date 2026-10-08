import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import check
import settings
from engine import LLM,cost_rmb,APIError
from provider import normalize_api_base,provider_settings,chat_payload,fetch_model_ids
from setup_gui import provider_form_settings
from storage import Store,BudgetExceeded


def config():
    c=json.loads((settings.ROOT/'config.example.json').read_text(encoding='utf-8-sig'))
    c.update(api_base='https://example.com/vendor/v1',model='my-model',api_key='test-only')
    return c


class ProviderSettingsTests(unittest.TestCase):
    def test_full_endpoint_and_version_path_are_preserved(self):
        self.assertEqual(normalize_api_base(' https://example.com/vendor/v2/chat/completions/ '),
                         'https://example.com/vendor/v2')
        self.assertEqual(normalize_api_base('https://example.com/api/'),'https://example.com/api')
        self.assertEqual(normalize_api_base('http://[::1]:8080/v1'),'http://[::1]:8080/v1')
        self.assertEqual(normalize_api_base('http://localhost:1234/v1'),'http://localhost:1234/v1')

    def test_invalid_and_nonlocal_plaintext_addresses_rejected(self):
        for value in ('example.com/v1','http://example.com/v1','https://user:key@example.com',
                      'https://example.com?key=secret','https://example.com/#x',
                      'https://example.com:bad/v1','https://example.com/\nmodel',''):
            with self.subTest(value=value),self.assertRaises(ValueError):
                normalize_api_base(value)

    def test_legacy_prices_and_inferera_behavior_survive_upgrade(self):
        c=config()
        c['api_base']='https://api.inferera.com/v1'
        c.pop('pricing_currency')
        c['input_usd_per_million']=c.pop('input_price_per_million')
        c['output_usd_per_million']=c.pop('output_price_per_million')
        updated=provider_settings(c)
        self.assertEqual(updated['pricing_currency'],'USD')
        self.assertEqual(updated['input_price_per_million'],c['input_usd_per_million'])
        self.assertEqual(updated['api_extra_body'],{'thinking':{'type':'disabled'}})
        self.assertAlmostEqual(cost_rmb(updated,1000,100),cost_rmb(c,1000,100))

    def test_new_provider_uses_generic_payload_and_optional_parameters(self):
        c=provider_settings(config())
        self.assertNotIn('thinking',chat_payload(c,[],80))
        c['api_extra_body']={'temperature':None,'enable_thinking':False,'top_p':0.9}
        payload=chat_payload(c,[],80)
        self.assertNotIn('temperature',payload)
        self.assertFalse(payload['enable_thinking'])
        self.assertEqual(payload['top_p'],0.9)
        self.assertEqual(payload['max_tokens'],80)
        for key in ('model','messages','stream','max_tokens','max_completion_tokens'):
            with self.subTest(key=key),self.assertRaises(ValueError):
                provider_settings(dict(c,api_extra_body={key:None}))

    def test_per_request_thinking_does_not_modify_chat_defaults(self):
        c=provider_settings(dict(config(),api_extra_body={'thinking':{'type':'disabled'},'temperature':0.6}))
        override={'thinking':{'type':'enabled'},'reasoning_effort':'low'}
        payload=chat_payload(c,[],2048,extra_body=override)
        self.assertEqual(payload['thinking'],{'type':'enabled'})
        self.assertEqual(payload['reasoning_effort'],'low')
        self.assertEqual(payload['temperature'],0.6)
        self.assertEqual(chat_payload(c,[],80)['thinking'],{'type':'disabled'})
        self.assertNotIn('reasoning_effort',chat_payload(c,[],80))
        self.assertEqual(override,{'thinking':{'type':'enabled'},'reasoning_effort':'low'})

    def test_prices_and_extra_json_are_validated(self):
        for changes in ({'input_price_per_million':-1},{'output_price_per_million':float('nan')},
                        {'pricing_currency':'EUR'},{'api_extra_body':[]},
                        {'api_extra_body':{'temperature':float('inf')}}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                provider_settings(dict(config(),**changes))

    def test_cny_usd_and_free_costs(self):
        c=dict(config(),input_price_per_million=1,output_price_per_million=2,
               usd_to_rmb=7,cost_margin=1.2)
        self.assertAlmostEqual(cost_rmb(c,1_000_000,500_000),16.8)
        c['pricing_currency']='CNY'
        self.assertAlmostEqual(cost_rmb(c,1_000_000,500_000),2.4)
        c.update(input_price_per_million=0,output_price_per_million=0)
        self.assertEqual(cost_rmb(c,1_000_000,500_000),0)

    def test_gui_can_save_new_host_model_and_cny_prices(self):
        values={'api_base':'https://new.example/v1/chat/completions','api_key':'new-key',
                'model':'custom-model','pricing_currency':'人民币 (CNY)',
                'input_price_per_million':'2','output_price_per_million':'8',
                'usd_to_rmb':'7','daily_budget_rmb':'2'}
        c=provider_form_settings(config(),values,'{}')
        self.assertEqual(c['api_base'],'https://new.example/v1')
        self.assertEqual(c['model'],'custom-model')
        self.assertEqual(c['pricing_currency'],'CNY')
        self.assertEqual(c['output_price_per_million'],8)
        self.assertNotIn('new-key',json.dumps(c))
        self.assertNotIn('api_key',c)
        with self.assertRaises(ValueError):
            provider_form_settings(c,dict(values,input_price_per_million='abc'),'{}')
        with self.assertRaises(ValueError):
            provider_form_settings(c,dict(values,api_key=''),'{}')
        self.assertEqual(provider_form_settings(c,dict(values,api_base='http://127.0.0.1:1234/v1',
                         api_key='',input_price_per_million='0'), '')['input_price_per_million'],0)

    def test_settings_load_local_api_without_key_but_remote_requires_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            c=dict(config(),api_base='http://127.0.0.1:1234/v1')
            root.joinpath('config.json').write_text(json.dumps(c),encoding='utf-8')
            root.joinpath('persona.txt').write_text('鲸鱼娘',encoding='utf-8')
            with patch.object(settings,'ROOT',root),patch.dict(os.environ,{},clear=True):
                self.assertEqual(settings.load_settings()['api_key'],'')
                c['api_base']='https://example.com/v1'
                root.joinpath('config.json').write_text(json.dumps(c),encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'密钥'):
                    settings.load_settings()

    def test_teaching_profile_settings_and_invalid_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            c=dict(config(),api_base='http://127.0.0.1:1234/v1',teaching_max_tokens=4096,teaching_timeout_seconds=120)
            root.joinpath('persona.txt').write_text('鲸鱼娘',encoding='utf-8')
            with patch.object(settings,'ROOT',root),patch.dict(os.environ,{},clear=True):
                root.joinpath('config.json').write_text(json.dumps(c),encoding='utf-8')
                loaded=settings.load_settings()
                self.assertEqual(loaded['teaching_max_tokens'],4096)
                self.assertEqual(loaded['teaching_timeout_seconds'],120)
                self.assertEqual(loaded['teaching_extra_body'],{'thinking':{'type':'enabled'},'reasoning_effort':'high'})
                for change in ({'teaching_max_tokens':8193},{'teaching_max_tokens':True},
                               {'teaching_timeout_seconds':0},{'teaching_timeout_seconds':float('nan')},
                               {'teaching_extra_body':{'model':'unsafe-override'}}):
                    with self.subTest(change=change):
                        root.joinpath('config.json').write_text(json.dumps(dict(c,**change)),encoding='utf-8')
                        with self.assertRaises(ValueError):
                            settings.load_settings()

    def test_gui_saves_separate_teaching_budget_and_timeout(self):
        values={'api_base':'https://new.example/v1','api_key':'test-only','model':'test-model',
                'pricing_currency':'CNY','input_price_per_million':'1','output_price_per_million':'2',
                'usd_to_rmb':'7','daily_budget_rmb':'2','teaching_max_tokens':'4096','teaching_timeout_seconds':'120'}
        c=provider_form_settings(config(),values,'{}')
        self.assertEqual(c['teaching_max_tokens'],4096)
        self.assertEqual(c['teaching_timeout_seconds'],120)
        for change in ({'teaching_max_tokens':'8193'},{'teaching_timeout_seconds':'NaN'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                provider_form_settings(c,dict(values,**change),'{}')


class ProviderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received=[]
        self.headers=[]
        self.model_reads=0
        self.response=None
        async def chat(request):
            self.received.append(await request.json())
            self.headers.append(dict(request.headers))
            return web.json_response(self.response or {'choices':[{'message':{'content':'鲸鱼娘来了'}}],
                                      'usage':{'prompt_tokens':10,'completion_tokens':5}})
        async def models(request):
            self.model_reads+=1
            return web.json_response({'data':[{'id':'my-model'},{'id':'another-model'}]})
        async def redirect(request):
            raise web.HTTPFound('/vendor/v1/models')
        app=web.Application()
        app.router.add_post('/vendor/v1/chat/completions',chat)
        app.router.add_get('/vendor/v1/models',models)
        app.router.add_get('/redirect/models',redirect)
        self.server=TestServer(app)
        await self.server.start_server()
        self.c=dict(config(),api_base=str(self.server.make_url('/vendor/v1/chat/completions')))
        self.store=Store(':memory:')
        self.session=aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()
        self.store.close()

    async def test_custom_api_and_manual_model_reach_real_local_endpoint(self):
        text=await LLM(self.c,self.store,self.session).chat([{'role':'user','content':'你好'}])
        self.assertEqual(text,'鲸鱼娘来了')
        self.assertEqual(self.received[0]['model'],'my-model')
        self.assertEqual(self.headers[0]['Authorization'],'Bearer test-only')
        self.assertNotIn('thinking',self.received[0])
        self.assertEqual(self.store.usage()['calls'],1)
        models=await asyncio.to_thread(fetch_model_ids,self.c['api_base'],self.c['api_key'])
        self.assertIn('my-model',models)

    async def test_free_local_api_has_no_auth_and_keeps_call_limit(self):
        self.c.update(api_key='',input_price_per_million=0,output_price_per_million=0,daily_call_limit=1)
        llm=LLM(self.c,self.store,self.session)
        await llm.chat([{'role':'user','content':'你好'}])
        self.assertNotIn('Authorization',self.headers[0])
        self.assertEqual(self.store.usage()['cost'],0)
        with self.assertRaises(BudgetExceeded):
            await llm.chat([{'role':'user','content':'再来'}])
        self.assertEqual(len(self.received),1)

    async def test_thinking_is_scoped_to_ai_call_and_accounted_for(self):
        llm=LLM(dict(self.c,api_extra_body={'thinking':{'type':'disabled'}}),self.store,self.session)
        await llm.chat([{'role':'user','content':'guess a word'}],'wordle_ai',max_tokens=2048,
            extra_body={'thinking':{'type':'enabled'},'reasoning_effort':'low'})
        await llm.chat([{'role':'user','content':'hello'}])
        self.assertEqual(self.received[0]['reasoning_effort'],'low')
        self.assertEqual(self.received[0]['thinking'],{'type':'enabled'})
        self.assertEqual(self.received[1]['thinking'],{'type':'disabled'})
        self.assertNotIn('reasoning_effort',self.received[1])
        row=self.store.db.execute("SELECT * FROM calls WHERE kind='wordle_ai'").fetchone()
        self.assertEqual(row['output_tokens'],5)
        self.assertGreater(row['charged'],0)

    async def test_invalid_request_override_cannot_spend_budget_or_replace_messages(self):
        with self.assertRaises(ValueError):
            await LLM(self.c,self.store,self.session).chat([],extra_body={'messages':[]})
        self.assertEqual(self.store.usage()['calls'],0)
        self.assertEqual(self.received,[])

    async def test_reasoning_only_response_keeps_its_fee_without_automatic_retry(self):
        self.response={'choices':[{'message':{'content':'','reasoning_content':'test-only reasoning'},'finish_reason':'length'}],
                       'usage':{'prompt_tokens':10,'completion_tokens':2000}}
        with self.assertRaisesRegex(APIError,'只返回了思考内容'):
            await LLM(self.c,self.store,self.session).chat([{'role':'user','content':'guess'}],'wordle_ai',max_tokens=2048,
                extra_body={'thinking':{'type':'enabled'},'reasoning_effort':'low'})
        self.assertEqual(len(self.received),1)
        self.assertAlmostEqual(self.store.usage()['cost'],cost_rmb(self.c,10,2000))

    async def test_optional_catalogue_can_be_absent_and_never_follows_redirects(self):
        with self.assertRaisesRegex(ValueError,'不提供模型列表'):
            await asyncio.to_thread(fetch_model_ids,str(self.server.make_url('/missing')),self.c['api_key'])
        with self.assertRaisesRegex(ValueError,'HTTP 302'):
            await asyncio.to_thread(fetch_model_ids,str(self.server.make_url('/redirect')),self.c['api_key'])
        self.assertEqual(self.model_reads,0)

    async def test_live_check_does_not_require_model_listing(self):
        # Simulate a provider whose optional model catalogue is unsupported.
        with tempfile.TemporaryDirectory() as directory:
            c=dict(self.c,discord_token='',servers=[],persona='鲸鱼娘')
            output=io.StringIO()
            with patch.object(check,'ROOT',Path(directory)),patch.object(check,'load_settings',return_value=c),\
                    patch.object(check,'fetch_model_ids',side_effect=ValueError('不提供模型列表')),\
                    contextlib.redirect_stdout(output):
                await check.check(live=True)
            self.assertIn('鲸鱼娘来了',output.getvalue())
            self.assertEqual(len(self.received),1)


if __name__=='__main__':
    unittest.main()
