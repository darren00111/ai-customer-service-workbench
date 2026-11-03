"""Offline checks for the reduced existing desktop product, never a live send."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace


def run_checks():
    from .config import StoreRegistry, StoreSpec
    from .platforms import list_platforms, platform_info
    from .service.workbench import Workbench
    from .service.workbench_api import WorkbenchAPI
    from .desktop import DesktopAPI, UI_DIR
    from .multistore import MultiStoreSupervisor
    from .service.qwen import ModelManager
    from merchant import default_manifest
    import yaml

    passed=[]
    def check(label,value):
        if not value:
            raise AssertionError(label)
        passed.append(label)
    def rejected(label,fn):
        try:
            fn()
        except ValueError:
            passed.append(label)
        else:
            raise AssertionError(label)

    with tempfile.TemporaryDirectory(prefix='feige-community-check-') as temporary:
        root=Path(temporary)
        stores=root/'stores'
        registry=StoreRegistry(root/'registry.json',stores)
        workbench=Workbench(root/'reception',stores,registry)
        bridge=WorkbenchAPI(workbench)
        supervisor=MultiStoreSupervisor(registry=registry)
        desktop=DesktopAPI(supervisor,SimpleNamespace(status=lambda:{'embedded':False}),bridge)
        try:
            check('平台列表仅抖店',[p['platform'] for p in list_platforms()]==['feige'])
            for platform in ('taobao','pinduoduo','xiaohongshu'):
                check(platform+'创建入口拒绝',not desktop.add_store(platform,'测试店')['ok'])
                rejected(platform+'配置校验拒绝',lambda p=platform:platform_info(p))
                rejected(platform+'商家文件拒绝',lambda p=platform:default_manifest('other','测试',platform_type=p))
            check('空店铺启动不导入主版本资料',workbench.stores()==[])
            added=desktop.add_store('feige','合成社区店铺')
            check('原桌面入口能添加抖店',added['ok'])
            store_id=added['store_id']
            spec=registry.load()[0]
            check('只创建一家店铺',len(registry.load(enabled_only=False))==1)
            check('社区独立调试端口',spec.cdp_port==19422)
            check('默认不自动发送不自动启动',not spec.auto_start and not spec.runtime_overrides['auto_send'])
            check('创建不触发真实登录或接待',not supervisor.agents)
            check('桌面第二店铺拒绝',not desktop.add_store('feige','第二家')['ok'])
            check('试聊入口第二店铺拒绝',not bridge.create_store('second','第二家')['ok'])
            check('拒绝后未残留第二店资料',len(list(stores.iterdir()))==1)
            second=StoreSpec('second','第二家',19423,'data/profiles/second')
            rejected('注册表保存两店拒绝',lambda:registry.save([spec,second]))
            rejected('批量生成两店拒绝',lambda:registry.scaffold(2))
            rejected('监督器直接传两店拒绝',lambda:MultiStoreSupervisor(specs=[spec,second]))
            check('原试聊引擎可用且有缺资料回退',workbench.chat(store_id,'这个多少钱')['action'] in ('handoff','ask_followup'))
            check('关闭自动回复设置可用',desktop.configure_auto_reply(store_id,False)['ok'])
            check('未配置模型不能开启自动发送',not desktop.configure_auto_reply(store_id,True,True)['ok'])
            workbench.models=SimpleNamespace(status=lambda:{'enabled':True,'connected':True})
            knowledge=workbench.knowledge(store_id)
            version=knowledge.stage('products',[{'product_id':'A','title':'合成验收商品','price':199,'stock':1}],
                                    '离线验收构造资料')
            knowledge.activate(version,reviewed=True)
            check('未核对不能开启自动回复',not desktop.configure_auto_reply(store_id,True,False)['ok'])
            check('准备完成后可以启用原自动回复',desktop.configure_auto_reply(store_id,True,True)['ok'])
            check('原引擎自动发送配置真实生效',registry.load()[0].runtime_overrides['auto_send'])
            check('启用模式仍不自动启动',not supervisor.agents)
            desktop.configure_auto_reply(store_id,False)
            check('接口不能打开其他平台',not desktop.choose_qianniu_client()['ok'] and not bridge.choose_qianniu_client()['ok'])
            check('模型凭证空间与主版本隔离',ModelManager()._path.parent.name=='FeigeCustomerServiceCommunity')
            for name in ('index.html','app.js','workbench.html','workbench.js'):
                check('原界面资源 '+name,(UI_DIR/name).is_file())
            check('真实飞鸽运行器保留',__import__('app.agent',fromlist=['FeigeAgent']).FeigeAgent is not None)
            # Disabled shops still consume the sole community slot.
            manifest_path=stores/store_id/'merchant.yaml'
            manifest=yaml.safe_load(manifest_path.read_text(encoding='utf-8'));manifest['automation']['enabled']=False
            manifest_path.write_text(yaml.safe_dump(manifest,allow_unicode=True),encoding='utf-8')
            check('停用店铺仍占名额',not bridge.create_store('second','第二家')['ok'])
            check('停用店铺在完整注册表中保留',len(registry.load(enabled_only=False))==1)
            forged=root/'forged.json'
            from dataclasses import asdict
            forged.write_text(json.dumps({'stores':[asdict(spec),asdict(second)]}),encoding='utf-8')
            rejected('手改两店注册表加载拒绝',lambda:StoreRegistry(forged).load())
        finally:
            supervisor.shutdown();bridge.shutdown()
        # Separate Workbench instances sharing a registry cannot race past capacity.
        race_stores=root/'race-stores'
        race_registry=StoreRegistry(root/'race.json',race_stores)
        a=Workbench(root/'race-a',race_stores,race_registry)
        b=Workbench(root/'race-b',race_stores,race_registry)
        def create(pair):
            w,key=pair
            try:
                w.create_store(key,key)
                return True
            except ValueError:
                return False
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(create,[(a,'one'),(b,'two')]))
            check('并发创建只有一家成功',sum(results)==1 and len(race_registry.load())==1)
        finally:
            a.close_connections();b.close_connections()
    return {'ok':True,'checks':passed,'count':len(passed),'edition':'community',
            'max_stores':1,'platforms':['feige'],'customer_messages_sent':False}
