# -*- coding: utf-8 -*-
import sys, os
sys.stdout.reconfigure(encoding='utf-8')

from sodus import coordinator, policy

texts = [
    ('文明六地图', '帮我做一个调研全国数据后参考文明六的风格的地图，要求能查阅任何一座城市或小镇的任何公开数据，可参考指标有文明六已有的一些指标，比如生产力，金钱等等，要求所有数据真实'),
    ('infra学习资料', '审阅并优化我在B盘的infra全套学习资料'),
]

for label, text in texts:
    a = policy.assess(text, review=True)
    p = coordinator.plan(text)
    j = a['jev']

    print(f'=== {label} ===')
    print(f'  字数: {a["chars"]}')
    print(f'  规则层 -> difficulty={a["rules"]["difficulty"]} kind={a["rules"]["kind"]} conf={a["rules"]["confidence"]:.2f}')
    print(f'  信号:  {a["signals"]}')
    if j['ok']:
        agree = j["agreement"]
        print(f'  Jev   -> difficulty={j["difficulty"]} (conf={j["difficulty_confidence"]:.2f})  kind={j["kind"]} (conf={j["kind_confidence"]:.2f})')
        print(f'  Jev needs: {j["needs"]}')
        print(f'  一致性: difficulty={agree["difficulty"]}  kind={agree["kind"]}')
    else:
        print(f'  Jev   -> 未运行 ({j.get("unavailable")})')
    print(f'  最终  -> difficulty={a["difficulty"]}/5  kind={a["kind"]}  needs={a["needs"]}  basis={a["basis"]}  conf={a["confidence"]:.2f}')
    print(f'  规划  -> executor={p.executor_route}  effort={p.effort}')
    print(f'  审查  -> review_kind={p.review_kind}  reviewer={p.reviewer_route}')
    print(f'  澄清  -> needs_clarification={p.needs_clarification}')
    print()
