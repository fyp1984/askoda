# -*- coding: utf-8 -*-
"""入口侧敏感信息识别与掩码（PRD §9.2「敏感信息脱敏要求」）

覆盖范围（PRD 列举 + 国内常见证件/账号形态）
--------------------------------------------
手机号、身份证号、银行卡号、客户姓名、家庭住址、账号标识、邮箱。

处置口径
--------
1. 识别到的敏感片段 → **掩码展示**（普通查看页面不出现明文）。
2. 原文不落日志、不随分析链路外传；`demand.get` 默认只返回掩码版。
3. 命中情况汇总进需求单 `sensitivity` 字段，供入口侧风控与人工复核。

设计取向
--------
规则式识别，**宁可多报不可漏报**：入口侧漏一个身份证号的代价，远高于多提示一次。
因此地址类用「关键词 + 长度」双条件，姓名类用「称谓前缀 / 姓氏表」约束，减少误伤。
"""
import re

# --- 基础模式 ---------------------------------------------------------------
_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_IDCARD = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
_BANKCARD = re.compile(r"(?<!\d)(\d{16,19})(?!\d)")
_EMAIL = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")

# 地址：省/市/区/县 + 街道/路/号/栋/室/楼 等
_ADDRESS = re.compile(
    r"[\u4e00-\u9fa5]{2,8}(?:省|市|自治区)"
    r"[\u4e00-\u9fa5]{0,12}(?:市|区|县|镇|乡|街道)"
    r"[\u4e00-\u9fa5\d]{0,20}(?:路|街|巷|弄|号|栋|幢|单元|室|楼)"
)

# 姓名：称谓前缀 + 中文姓名（2–4 字）
_NAME_WITH_TITLE = re.compile(
    r"(客户|用户|会员|联系人|负责人|收件人|持卡人|本人)[:：\s]*([\u4e00-\u9fa5]{2,4})"
)

# 姓名：姓氏 + 职务后缀（「王经理」「李女士」这类强信号，误伤率低）
_NAME_WITH_POST = re.compile(
    r"([\u4e00-\u9fa5]{1,2})(先生|女士|经理|总监|主任|主管|老师)"
)

# 账号标识：账号/账户/卡号/工号/客户号 等关键词 + 数字字母串
_ACCOUNT_ID = re.compile(
    r"(账号|帐号|账户|卡号|工号|客户号|会员号|编号)[:：\s]*([A-Za-z0-9\-_]{6,})"
)

# 常见姓氏（用于「姓名」无称谓前缀时的兜底判定，控制误伤）
_SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚"
)


def _mask_phone(m):
    s = m.group(1)
    return s[:3] + "****" + s[-4:]


def _mask_idcard(m):
    s = m.group(1)
    return s[:6] + "*" * 8 + s[-4:]


def _mask_bankcard(m):
    s = m.group(1)
    return "*" * (len(s) - 4) + s[-4:]


def _mask_email(m):
    local, domain = m.group(1), m.group(2)
    head = local[:1] if local else "*"
    return head + "***@" + domain


def _mask_address(m):
    s = m.group(0)
    if len(s) <= 6:
        return s[:2] + "***"
    return s[:6] + "***" + (s[-1] if s[-1] in "室号楼栋" else "")


def _mask_name_with_title(m):
    return m.group(1) + "：" + m.group(2)[:1] + "*" * (len(m.group(2)) - 1)


def _mask_name_with_post(m):
    return m.group(1)[:1] + "**" + m.group(2)


def _guard_surname_post(m):
    return bool(m.group(1)) and m.group(1)[0] in _SURNAMES


def _mask_account(m):
    s = m.group(2)
    return m.group(1) + "：" + s[:2] + "*" * (len(s) - 2)


# 执行顺序：先长后短——身份证须排在银行卡之前，否则 18 位身份证会被银行卡规则抢走
def _guard_surname(m):
    """姓名规则准入：首字必须是常见姓氏。

    不加这道闸会把「会员复购情况分析」里的「会员复购」误判成人名（实测踩到过）。
    姓名的误伤代价不对称：漏一个真名只是少掩一次，错掩一个普通词会让需求单读不通。
    """
    name = m.group(2) or ""
    return len(name) >= 2 and name[0] in _SURNAMES


_RULES = [
    ("身份证号", _IDCARD, _mask_idcard, None),
    ("银行卡号", _BANKCARD, _mask_bankcard, None),
    ("手机号", _PHONE, _mask_phone, None),
    ("邮箱", _EMAIL, _mask_email, None),
    ("家庭住址", _ADDRESS, _mask_address, None),
    ("客户姓名", _NAME_WITH_TITLE, _mask_name_with_title, _guard_surname),
    ("客户姓名", _NAME_WITH_POST, _mask_name_with_post, _guard_surname_post),
    ("账号标识", _ACCOUNT_ID, _mask_account, None),
]


def scan(text):
    """识别敏感片段，返回 (命中明细, 类型统计)。

    命中明细的 `start/end` 基于**原文**，供前端做受控高亮。

    同一段文本只归**优先级最高的一条规则**（按 `_RULES` 顺序先占位）：身份证 18 位同时
    符合"16–19 位数字"的银行卡形态，若不去重会被重复计入两类敏感信息。
    """
    text = text or ""
    hits = []
    covered = []  # 已占用的字符区间
    for label, pattern, _fn, guard in _RULES:
        for m in pattern.finditer(text):
            s, e = m.start(), m.end()
            if e <= s:
                continue
            if any(s < ce and cs < e for cs, ce in covered):
                continue  # 与已命中区间重叠 → 让位给优先级更高的规则
            if guard and not guard(m):
                continue
            covered.append((s, e))
            hits.append(
                {
                    "type": label,
                    "start": s,
                    "end": e,
                    "preview": _preview(label, m.group(0)),
                }
            )
    hits.sort(key=lambda h: (h["start"], h["end"]))
    summary = {}
    for h in hits:
        summary[h["type"]] = summary.get(h["type"], 0) + 1
    return hits, summary


def _preview(label, raw):
    """给复核人员看的最小预览（本身就是掩码态）。"""
    if label == "身份证号":
        return raw[:6] + "********" + raw[-4:]
    if label == "银行卡号":
        return "****" + raw[-4:]
    if label == "手机号":
        return raw[:3] + "****" + raw[-4:]
    if label == "邮箱":
        return raw.split("@")[0][:1] + "***@" + raw.split("@")[-1]
    if len(raw) <= 4:
        return raw[:1] + "*"
    return raw[:2] + "***"


def mask(text):
    """返回掩码后的文本。

    按规则优先级依次替换，已替换的片段用占位符保护，避免被后续规则二次命中；
    `guard` 不通过的匹配**原样保留**（与 `scan` 的准入判断保持一致，避免
    "报出来命中了、但文本里没掩到"的不一致）。
    """
    text = text or ""
    protected = {}
    out = text

    def _stash(value):
        token = "\x00%d\x00" % len(protected)
        protected[token] = value
        return token

    for _label, pattern, fn, guard in _RULES:

        def repl(m, f=fn, g=guard):
            if g and not g(m):
                return m.group(0)
            return _stash(f(m))

        out = pattern.sub(repl, out)
    for token, value in protected.items():
        out = out.replace(token, value)
    return out


def mask_payload(payload, fields):
    """对指定字段做掩码，返回新 dict 与被掩码的字段名列表。

    原文不在此处保留：调用方负责把原文写库，掩码版用于对外返回。
    """
    masked = dict(payload)
    masked_fields = []
    for f in fields:
        if isinstance(payload.get(f), str) and payload[f].strip():
            hits, _ = scan(payload[f])
            if hits:
                masked[f] = mask(payload[f])
                masked_fields.append(f)
    return masked, masked_fields


def summarize_text(text):
    """对一段文本做敏感扫描 + 掩码，返回 (掩码文本, 命中的类型统计)。"""
    hits, summary = scan(text)
    return (mask(text) if hits else text), summary
