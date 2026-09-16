from __future__ import annotations

import json
from typing import Any


def build_understanding_prompt(article: str, *, source: str, url: str = "") -> str:
    return f"""STAGE=UNDERSTANDING_MAP\n你现在只做“文章理解地图”，不要生成训练题，也不要个性化到读者。

采用三组方法：
1. 先理解再提问：先把作者的论点表述到作者本人会认为准确，再区分核心主张、支撑理由、例子和结论。
2. 建立理解地图：识别 central thesis、关键推理链、案例/证据、适用边界与作者最终落点。第一道题以后必须来自 central thesis，而不是孤立事实。
3. 反向提纲：按语义而不是标题切分文章，判断每一块在整篇论证里的作用。标题只是一个不可信提示，不能自动当作核心。

硬规则：
- 先看文章开头提出了什么问题，结尾回到了什么问题；首尾闭环通常比标题更能说明作者真正要讲什么。
- 对每个醒目的案例做“删除测试”：删掉它以后，如果文章大部分论证仍成立，它就是例子，不是核心命题。
- 若多个案例在讲同一种更高层机制，central_thesis 必须写那个共同机制，不能任选一个案例代替。
- 明确判断标题主题在全文中是 core、example、hook 还是 mixed，并说明依据。
- 只依据文章正文；不要补外部事实，不要评价作者对错。

只返回 JSON：
{{
  "central_thesis":"一句话写整篇文章真正的中心主张，保持作者原始强度",
  "neutral_core_problem":"把 central_thesis 改写成不泄露作者答案、可供读者先独立判断的核心问题",
  "argument_spine":["按顺序写3到7个关键论证步骤"],
  "examples":[{{"example":"案例/话题","role":"它在论证中证明或说明什么","removable":true}}],
  "title_role":"core|example|hook|mixed",
  "title_role_reason":"为什么这样判断",
  "author_reasoning":"忠实重建作者考虑的变量、因果链、类比、反例、条件和回扣过程",
  "author_conclusion":"作者最终结论"
}}

来源：{source}
链接：{url}

文章正文：
{article}
"""


def build_card_prompt(article: str, understanding: dict[str, Any], *, source: str, url: str = "") -> str:
    map_json = json.dumps(understanding, ensure_ascii=False, indent=2)
    return f"""STAGE=BLIND_CARD\n根据已经完成的“文章理解地图”，生成一张 blind-first 思维训练卡。你的任务不是重新判断文章核心，而是忠实围绕 neutral_core_problem 设计一个读者在看原文前就能回答的问题。

最重要的门槛：
- question 必须直接训练 neutral_core_problem。
- question 隐去文章标题后仍然应该成立；如果只有看到标题才觉得题目合理，说明你问偏了。
- 当 title_role 是 example 或 hook 时，禁止把标题中的具体话题当成问题中心。它最多只能作为 background 里的一个例子。
- 当文章用了多个案例说明同一个机制时，background 应给足够具体的案例帮助进入，但 question 要问案例背后的共同机制。
- 不要把读者丢进他没经历过的抽象角色；不要让他扮演老板、CEO、投资经理、政策制定者。
- background 约180～380个中文字符，只提供独立判断所需的事实、冲突和必要解释，不泄露作者结论或关键转折。
- 问题所需事实必须已经在 background 里；缺失且会改变判断的信息要明确说不知道。
- question 最多2句，只问一个核心判断，必须以“仅看上面的信息，”开头。可以追问一条最关键的缺失信息，但不要机械地每题都问“你还缺什么信息”。
- 只有文章与用户当前真实问题确有同一个具体决策变量时，why_selected 才做个性化；否则只写它训练什么判断。
- 用自然短句中文，不要堆抽象术语。

文章理解地图：
{map_json}

只返回 JSON：
{{
  "title":"文章标题",
  "topic":"简短主题；应反映 central_thesis，而不是机械照抄标题",
  "estimated_minutes":8,
  "why_selected":"最多一句",
  "background":"blind-first 背景",
  "question":"blind-first 核心问题"
}}

来源：{source}
链接：{url}

文章正文仅用于核对具体事实，不得推翻理解地图后重新被标题带偏：
{article}
"""
