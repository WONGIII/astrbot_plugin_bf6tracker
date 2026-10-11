# -*- coding: utf-8 -*-
"""
astrbot_plugin_bf6tracker — 战地6(Battlefield 6)战绩查询插件

数据来源: OpenBF6Tracker (https://battlefield.977744.xyz)
指令:   /bf6 <玩家名>          生涯战绩总览图
        /bf6战报 <玩家名> [页]  最新对局战报图(分页)
        /bf6下一页 / /bf6上一页 翻看后续/前面的战报
智能体: bf6_stats / bf6_battle_reports 两个 LLM 工具，可被大模型自动调用
出图:   AstrBot 内置 html_render (HTML+Jinja2 文转图)，支持暗色/浅色主题
"""

import asyncio
import datetime
import math
import re
from pathlib import Path

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

DEFAULT_API_BASE = "https://battlefield.977744.xyz"
GT_PLAYER_SEARCH = "https://api.gametools.network/bf6/player/"
PLUGIN_DIR = Path(__file__).parent
TPL_DIR = PLUGIN_DIR / "templates"
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

# ==================== 数据转换工具 ====================

CAT_ZH = {
    "Assault Rifles": "突击步枪", "Carbines": "卡宾枪", "SMG-PDWs": "冲锋枪/PDW",
    "Shotguns": "霰弹枪", "Machine Guns": "机枪", "DMRs": "精确射手步枪",
    "Rifles": "狙击步枪", "Pistols": "手枪",
    "Air Combat": "空战载具", "Ground Combat": "地面战斗载具",
    "Air Transport": "空中运输", "Ground Transport": "地面运输",
    "Assault": "突击兵", "Engineer": "工程兵", "Support": "支援兵", "Recon": "侦察兵",
    "Melee Light": "轻型近战", "Melee Heavy": "重型近战",
}
MODE_ZH = {
    "Conquest": "征服", "Breakthrough": "突破", "Rush": "突袭",
    "Team deathmatch": "团队死斗", "Squad deathmatch": "小队死斗",
    "Domination": "制霸", "Escalation": "升级", "Gauntlet": "试炼场",
    "Strikepoint": "强袭点", "King of the Hill": "占山为王",
    "Battle Royale": "大逃杀", "Redsec": "大逃杀",
    "Redsec Duo": "大逃杀双排", "Redsec Squad": "大逃杀四排",
}
KIT_ZH = {"Assault": "突击兵", "Engineer": "工程兵", "Support": "支援兵", "Recon": "侦察兵"}
PLATFORM_ZH = {"origin": "EA APP", "steam": "STEAM", "psn": "PSN", "xbox": "XBOX"}


def _n(v, d=0.0):
    """任意值 -> float"""
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return d


def _fv(v):
    """整数千分位"""
    return f"{int(_n(v)):,}"


def _valid(v) -> bool:
    """统计值有效且非零（用于过滤数据源缺失/为 0 的指标）"""
    s = str(v).replace(",", "").replace("%", "").strip()
    try:
        return s not in ("", "-", "0") and float(s) != 0
    except ValueError:
        return True  # 非 numeric(如 '胜 / 负')视为有效


def _fsec(sec):
    """秒 -> 'xh xxm' / 'xxm'"""
    sec = int(_n(sec))
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def build_overview(profile: dict, top_n: int = 8) -> dict:
    """profile API 返回 -> 总览模板数据"""
    data = profile["data"]
    segs = data["segments"]
    ov = next(s for s in segs if s["type"] == "overview")
    st = ov["stats"]
    g = lambda k: st.get(k, {}).get("displayValue", "0")
    pi = data["platformInfo"]

    # 综合数据网格：核心六项置前(橙色)，其余按优先级衔接；缺失/零值自动隐藏
    # 站点 API 目前返回 47 个 overview 字段，这里全部覆盖（零值/缺失自动隐藏）
    grid = [
        # —— 核心六项（橙色高亮）——
        ("K/D 比", g("kd"), True), ("击杀", g("kills"), True),
        ("KPM", g("killsPerMinute"), True), ("SPM", g("scorePerMinute"), True),
        ("胜率", g("wl"), True), ("游戏时长", g("timePlayed"), True),
        # —— 战斗 ——
        ("死亡", g("deaths"), False), ("总得分", g("score"), False),
        ("总场次", g("matchesPlayed"), False), ("胜 / 负", f"{g('wins')} / {g('losses')}", False),
        ("爆头数", g("headShots"), False), ("爆头率", g("headshotsPercent"), False),
        ("命中率", g("accuracy"), False), ("每场击杀", g("killsPerMatch"), False),
        ("DPM", g("damagePerMinute"), False), ("总伤害", g("damageDealt"), False),
        ("每场伤害", g("damagePerMatch"), False), ("击杀助攻", g("killAssists"), False),
        ("助攻", g("assists"), False), ("救援队友", g("revives"), False),
        ("治疗量", g("heals"), False), ("弹药补给", g("resupplies"), False),
        ("载具维修", g("repairs"), False), ("发现敌人", g("enemiesSpotted"), False),
        ("投掷物", g("thrownThrowables"), False), ("拆装置", g("gadgetsDestoyed"), False),
        ("处决", g("playerTakeDowns"), False), ("小队救人", g("squadmateRevive"), False),
        ("载具摧毁", g("vehiclesDestroyed"), False),
        ("救死扶伤击杀", g("saviorKills"), False), ("复仇击杀", g("avengerKills"), False),
        # —— 真人 / 步兵 ——
        ("真人击杀", g("humanKills"), False), ("真人 K/D", g("humanKd"), False),
        ("真人 KPM", g("humanKpm"), False), ("真人占比", g("humanPercent"), False),
        ("步兵 K/D", g("infantryKd"), False), ("步兵 K/D 率", g("infantryKillDeathRate"), False),
        ("最高连杀", g("highestKillStreak"), False),
        # —— 射击 / 移动 / 时长 ——
        ("射击数", g("shotsFired"), False), ("命中数", g("shotsHit"), False),
        ("行进距离", g("distanceTraveled"), False),
        ("驾驶时间", g("playtimeDriving"), False), ("飞行时间", g("playtimeFlying"), False),
        ("总 XP", g("xpTotal"), False), ("生涯等级", g("careerPlayerRank"), False),
    ]
    stats = [{"l": l, "v": v, "o": "o" if hot else ""}
             for l, v, hot in grid if _valid(v)]

    # 兵种
    kits = [s for s in segs if s["type"] == "kit"]
    total_t = sum(_n(s["stats"].get("timePlayed", {}).get("value")) for s in kits) or 1
    classes = []
    for s in sorted(kits, key=lambda x: -_n(x["stats"].get("timePlayed", {}).get("value"))):
        ss = s["stats"]
        en = s["metadata"].get("name", "")
        classes.append({
            "name": KIT_ZH.get(en, en), "en": en.upper(),
            "img": s["metadata"].get("imageUrl", ""),
            "kills": _fv(ss.get("kills", {}).get("value")),
            "kd": ss.get("kd", {}).get("displayValue", "-"),
            "kpm": ss.get("killsPerMinute", {}).get("displayValue", "-"),
            "time": _fsec(ss.get("timePlayed", {}).get("value")),
            "share": round(_n(ss.get("timePlayed", {}).get("value")) / total_t * 100, 1),
        })

    # 模式
    modes_raw = sorted(
        (s for s in segs if s["type"] == "gamemode"),
        key=lambda x: -_n(x["stats"].get("secondsPlayed", {}).get("value")))
    modes = []
    for s in modes_raw[:8]:
        ss = s["stats"]
        wp = _n(ss.get("winPercent", {}).get("value"))
        modes.append({
            "name": s["metadata"].get("name", ""), "img": s["metadata"].get("imageUrl", ""),
            "matches": _fv(ss.get("matches", {}).get("value")),
            "kills": _fv(ss.get("kills", {}).get("value")),
            "kd": ss.get("kd", {}).get("displayValue", "-"),
            "kpm": ss.get("kpm", {}).get("displayValue", "-"),
            "winpct": ss.get("winPercent", {}).get("displayValue", "-"),
            "winbar": max(4, min(100, wp)),
        })

    # 地图(排除 All 聚合行)
    maps_raw = sorted(
        (s for s in segs if s["type"] == "level" and s["metadata"].get("name") not in ("All", "")),
        key=lambda x: -_n(x["stats"].get("matchesPlayed", {}).get("value")))
    maps = []
    for s in maps_raw[:8]:
        ss = s["stats"]
        w, l = _n(ss.get("wins", {}).get("value")), _n(ss.get("losses", {}).get("value"))
        maps.append({
            "name": s["metadata"].get("name", ""), "img": s["metadata"].get("imageUrl", ""),
            "wins": int(w), "losses": int(l),
            "winpct": f"{(w / (w + l) * 100):.0f}%" if w + l else "-",
        })

    # 武器
    weps_top = sorted(
        (s for s in segs if s["type"] == "weapon"),
        key=lambda x: -_n(x["stats"].get("kills", {}).get("value")))[:top_n]
    maxk = max((_n(s["stats"].get("kills", {}).get("value")) for s in weps_top), default=0) or 1
    weapons = []
    for s in weps_top:
        ss = s["stats"]
        weapons.append({
            "name": s["metadata"].get("name", ""), "img": s["metadata"].get("imageUrl", ""),
            "cat": s["metadata"].get("category", ""),
            "kills": _fv(ss.get("kills", {}).get("value")),
            "kpm": ss.get("killsPerMinute", {}).get("displayValue", "-"),
            "acc": ss.get("accuracy", {}).get("displayValue", "-"),
            "hs": ss.get("headshots", {}).get("displayValue", "-"),
            "time": _fsec(ss.get("timeEquipped", {}).get("value")),
            "bar": round(_n(ss.get("kills", {}).get("value")) / maxk * 100, 1),
        })

    # 载具
    veh_top = sorted(
        (s for s in segs if s["type"] == "vehicle"),
        key=lambda x: -_n(x["stats"].get("kills", {}).get("value")))[:top_n]
    maxv = max((_n(s["stats"].get("kills", {}).get("value")) for s in veh_top), default=0) or 1
    vehicles = []
    for s in veh_top:
        ss = s["stats"]
        vehicles.append({
            "name": s["metadata"].get("name", ""), "img": s["metadata"].get("imageUrl", ""),
            "cat": s["metadata"].get("category", ""),
            "kills": _fv(ss.get("kills", {}).get("value")),
            "kpm": ss.get("killsPerMinute", {}).get("displayValue", "-"),
            "dest": _fv(ss.get("destroyed", {}).get("value")),
            "time": _fsec(ss.get("timeInVehicle", {}).get("value")),
            "bar": round(_n(ss.get("kills", {}).get("value")) / maxv * 100, 1),
        })

    # 装备：按击杀降序取前 8（不足 8 个时用 0 击杀的补位），无图标渲染占位块
    gad_all = [s for s in segs if s["type"] == "gadget"]
    gad_all.sort(key=lambda x: -_n(x["stats"].get("kills", {}).get("value")))
    gadgets = [{
        "name": s["metadata"].get("name", ""), "img": s["metadata"].get("imageUrl", ""),
        "kills": _fv(s["stats"].get("kills", {}).get("value")),
    } for s in gad_all[:8]]

    # 近战
    melee = []
    for s in (x for x in segs if x["type"] == "melee"):
        ss = s["stats"]
        melee.append({
            "name": s["metadata"].get("name", ""), "cat": s["metadata"].get("category", ""),
            "kills": _fv(ss.get("kills", {}).get("value")),
            "takedowns": _fv(ss.get("takedowns", {}).get("value")),
            "uses": _fv(ss.get("uses", {}).get("value")),
            "time": _fsec(ss.get("timeEquipped", {}).get("value")),
        })

    return {
        "player": {
            "name": pi.get("platformUserHandle", ""),
            "pid": pi.get("platformUserIdentifier", ""),
            "platform_label": PLATFORM_ZH.get(pi.get("platformSlug", ""), str(pi.get("platformSlug", "")).upper()),
            "avatar": pi.get("avatarUrl", ""),
            "rank": g("careerPlayerRank"),
            "rank_img": st.get("careerPlayerRank", {}).get("metadata", {}).get("imageUrl", ""),
        },
        "stats": stats, "classes": classes, "modes": modes,
        "maps": maps, "weapons": weapons, "vehicles": vehicles,
        "gadgets": gadgets, "melee": melee,
        "generated_at": datetime.datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M"),
    }


def _seg_item(item: dict) -> dict:
    """战报 metadata 分组项 -> 统一结构"""
    md = item.get("metadata", {}) or {}
    ss = {k: v.get("displayValue", "") for k, v in (item.get("stats", {}) or {}).items()}
    raw = {k: _n(v.get("value")) for k, v in (item.get("stats", {}) or {}).items()}
    return {"name": md.get("name", ""), "img": md.get("imageUrl", ""), "st": ss, "raw": raw}


# 战报分组里真正代表「本局用过」的字段。
# 注意：不包含 timePlayed / secondsPlayed / timeEquipped 这类计时器——
# 它们是累计值，任何一局都会变动，会放行大量「本局零数据」的条目。
_SEG_ACTIVE_KEYS = (
    "kills", "deaths", "assists", "killAssists", "revives", "heals", "resupplies",
    "repairs", "spots", "spotAssists", "uses", "spawns", "damage", "assistsDamage",
    "explosiveDamageWith", "headshots", "headshotKills", "takedowns",
    "matches", "matchesPlayed",
)


def _seg_active(x: dict) -> bool:
    """该条目在本局是否有实际数据（任一计数大于 0）"""
    raw = x.get("raw", {}) or {}
    return any(raw.get(k, 0) > 0 for k in _SEG_ACTIVE_KEYS)


def _seg_desc(x: dict, fields: list, empty: str = "-") -> str:
    """按 (标签, 字段名) 拼 desc，只显示数值大于 0 的项。

    直接用 displayValue 判断会踩坑：值为 0 时 displayValue 是字符串 "0"，
    在 Python 里是 truthy，会导致每一条都渲染成「击杀 0」。
    """
    raw = x.get("raw", {}) or {}
    st = x.get("st", {}) or {}
    parts = []
    for label, key in fields:
        if raw.get(key, 0) > 0:
            parts.append(f"{label} {st.get(key, int(raw[key]))}")
    return _join_parts(*parts) or empty


def _seg_filter(items: list, limit: int = 10) -> list:
    """过滤掉本局全零的条目并限制数量。

    差分数据里经常带上几十个「0 击杀 0 使用」的条目（例如装备段），
    全量渲染会把单张战报卡撑到几千像素，既慢又难读。
    """
    live = [x for x in items if _seg_active(x)]
    # 本局该分类全程零数据时直接返回空，模板会自动隐藏整个区块；
    # 不返回首项——那会渲染出「某某 本局未使用」这种没有信息量的条目。
    return live[:limit]


def _fmt_any_time(v) -> str:
    """秒数字符串 -> xh xm；非纯数字原样返回"""
    s = str(v).strip()
    if s and re.fullmatch(r"\d+(?:\.\d+)?", s):
        return _fsec(float(s))
    return s


def _join_parts(*parts: str) -> str:
    return " · ".join(x for x in parts if x)


def build_matches(matches: list, player: dict) -> dict:
    """matches API 返回 -> 战报模板数据"""
    out = []
    for m in matches:
        ovs = [s for s in m.get("segments", []) if s.get("type") == "overview"]
        if not ovs:
            continue
        ov = ovs[0]
        st = {k: v.get("displayValue", "") for k, v in ov.get("stats", {}).items()}
        raw = {k: _n(v.get("value")) for k, v in ov.get("stats", {}).items()}
        md = ov.get("metadata", {}) or {}

        # 战报 overview 是「差分」，只含本局变动的字段。门户等不计入官方统计的模式，
        # 整局数据只落在模式分组里，因此这里做分组汇总兜底，否则顶部会显示 0 击杀。
        mode_items = md.get("gamemodes", []) or []

        def _sum(key: str) -> float:
            return sum(_n((x.get("stats", {}) or {}).get(key, {}).get("value")) for x in mode_items)

        kills_v = raw.get("kills", 0) or _sum("kills")
        wins = int(raw.get("wins", 0) or _sum("wins"))
        losses = int(raw.get("losses", 0) or _sum("losses"))
        deaths_v = raw.get("deaths", 0) or _sum("deaths")
        secs_v = raw.get("timePlayed", 0) or _sum("secondsPlayed") or _sum("timePlayed")
        score_v = raw.get("score", 0) or _sum("score")
        kd_v = raw.get("kd", 0) or (kills_v / deaths_v if deaths_v else kills_v)
        kpm_v = raw.get("killsPerMinute", 0) or (kills_v / (secs_v / 60) if secs_v else 0)
        spm_v = raw.get("scorePerMinute", 0) or (score_v / (secs_v / 60) if secs_v else 0)
        hs_v = raw.get("headShots", 0) or _sum("headshotKills")
        result = "win" if wins > losses else ("loss" if losses > wins else "draw")
        # 与站点一致的「中途退出」判定：有击杀但没有胜负、模式分组也没有本局场次记录、
        # 顶层 matchesPlayed 也缺失 —— 说明这局没被上游计入正式统计。
        mode_matches = _sum("matches")
        if (kills_v > 0 and wins == 0 and losses == 0
                and mode_matches == 0 and raw.get("matchesPlayed", 0) == 0):
            result = "left"

        modes = sorted((_seg_item(x) for x in md.get("gamemodes", [])),
                       key=lambda x: -_n(x["st"].get("kills")))
        maps = sorted((_seg_item(x) for x in md.get("levels", [])),
                      key=lambda x: -_n(x["st"].get("matchesPlayed", x["st"].get("kills"))))
        # 过滤本局全零条目并限量，避免单张卡片被几十个「0 击杀」撑爆
        weps = _seg_filter(sorted((_seg_item(y) for y in md.get("weapons", [])),
                                  key=lambda x: -_n(x["st"].get("kills"))))
        vehs = _seg_filter(sorted((_seg_item(y) for y in md.get("vehicles", [])),
                                  key=lambda x: -_n(x["st"].get("kills"))), 8)
        gads = _seg_filter(sorted((_seg_item(y) for y in md.get("gadgets", [])),
                                  key=lambda x: -_n(x["st"].get("kills"))), 8)
        kits = _seg_filter([_seg_item(x) for x in md.get("kits", [])], 6)
        melees = _seg_filter(sorted((_seg_item(x) for x in md.get("melees", md.get("melee", []))),
                                    key=lambda x: -_n(x["st"].get("kills"))), 4)

        ts = (m.get("metadata", {}) or {}).get("timestamp", "")
        time_str = ""
        if ts:
            try:
                t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ_CN)
                time_str = t.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass

        # K/D 击杀占比（用于比例条）
        total_kd = kills_v + deaths_v
        kd_pct = round(kills_v / total_kd * 100) if total_kd else 0
        hs_pct = round(hs_v / kills_v * 100) if kills_v else 0

        out.append({
            "result": result,
            "result_label": {"win": "胜利", "loss": "战败", "draw": "平局", "left": "中途退出"}[result],
            "time": time_str,
            "map_name": maps[0]["name"] if maps else "",   # 差分里没有地图段时留空(模板会隐藏该行)
            "map_img": maps[0]["img"] if maps else "",
            "mode_name": modes[0]["name"] if modes else "",
            "mode_img": modes[0]["img"] if modes else "",
            "wins": wins, "losses": losses,
            "kd_pct": kd_pct, "kd_pct_loss": 100 - kd_pct,
            "stats": [
                {"l": "击杀", "v": _fv(kills_v), "o": "o"},
                {"l": "死亡", "v": _fv(deaths_v), "o": ""},
                {"l": "K/D", "v": f"{kd_v:.2f}", "o": "o"},
                {"l": "KPM", "v": f"{kpm_v:.2f}", "o": ""},
                {"l": "SPM", "v": f"{spm_v:.0f}", "o": ""},
                {"l": "得分", "v": _fv(score_v), "o": ""},
                {"l": "爆头", "v": _fv(hs_v), "o": ""},
                {"l": "爆头率", "v": f"{hs_pct}%", "o": ""},
                {"l": "时长", "v": _fsec(secs_v), "o": ""},
            ],
            "weapons": [{"name": w["name"], "img": w["img"],
                         "kills": w["st"].get("kills", "0"),
                         "kpm": w["st"].get("killsPerMinute", ""),
                         "acc": w["st"].get("accuracy", ""),
                         "time": _fmt_any_time(w["st"].get("timeEquipped", ""))} for w in weps],
            "vehicles": [{"name": v["name"], "img": v["img"],
                          "kills": v["st"].get("kills", "0"),
                          "time": _fmt_any_time(v["st"].get("timeInVehicle", ""))} for v in vehs],
            "gadgets": [{"name": gd["name"], "img": gd["img"],
                         "desc": _seg_desc(gd, [("击杀", "kills"), ("携带", "spawns"),
                                                ("使用", "uses"), ("索敌", "spots"),
                                                ("伤害", "damage")], "本局未使用")} for gd in gads],
            "kits": [{"name": KIT_ZH.get(k["name"], k["name"]), "img": k["img"],
                      "desc": _seg_desc(k, [("击杀", "kills"), ("死亡", "deaths"),
                                            ("救援", "revives"), ("治疗", "heals"),
                                            ("协助", "assists")], "本局未使用")} for k in kits],
            "modes": [{"name": x["name"], "img": x["img"],
                       "kills": x["st"].get("kills", "0")} for x in modes],
            "maps": [{"name": x["name"], "img": x["img"],
                      "kills": x["st"].get("matchesPlayed", x["st"].get("kills", ""))} for x in maps],
            "melee": [{"name": x["name"], "img": x["img"],
                       "desc": _seg_desc(x, [("击杀", "kills"), ("处决", "takedowns"),
                                             ("使用", "uses")], "本局未使用")} for x in melees],
        })
    return {"player": player, "matches": out}


# ==================== 插件主体 ====================

PAGE_WORDS_NEXT = ("下一页", "next", "下页")
PAGE_WORDS_PREV = ("上一页", "prev", "上页")


class BF6TrackerPlugin(Star):
    """战地6战绩查询插件 (OpenBF6Tracker)

    插件元数据见 metadata.yaml。AstrBot v3.5.19+ 自动识别继承 Star 的插件类。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 会话翻页记忆: unified_msg_origin -> {"identifier","name","platform_label","page"}
        self._sessions: dict[str, dict] = {}
        # 玩家名解析缓存: name_lower -> (identifier, platform)
        self._resolve_cache: dict[str, tuple[str, str]] = {}
        # 共享 HTTP 会话(连接池复用，避免每次请求重新 TLS 握手)
        self._http: aiohttp.ClientSession | None = None
        # 模板文件缓存(避免每次出图都读盘)
        self._tpl_cache: dict[str, str] = {}

    # ---------- 基础设施 ----------

    @property
    def _api_base(self) -> str:
        return str(self.config.get("api_base") or DEFAULT_API_BASE).rstrip("/")

    def _http_session(self) -> aiohttp.ClientSession:
        """惰性创建并复用同一个 HTTP 会话(连接池 + keep-alive)。

        原实现每次请求都新建 ClientSession，冷门玩家一次查询要建 3~5 个会话、
        反复做 TLS 握手，在移动网络/高延迟下非常慢。
        """
        if self._http is None or self._http.closed:
            timeout = aiohttp.ClientTimeout(
                total=int(self.config.get("timeout", 30)),
                connect=10,
                sock_read=int(self.config.get("timeout", 30)),
            )
            connector = aiohttp.TCPConnector(limit=16, ttl_dns_cache=300, enable_cleanup_closed=True)
            self._http = aiohttp.ClientSession(timeout=timeout, trust_env=True, connector=connector)
        return self._http

    async def _api_get(self, path: str, params: dict | None = None, timeout_s: float | None = None):
        session = self._http_session()
        try:
            req_timeout = aiohttp.ClientTimeout(total=timeout_s) if timeout_s else None
            async with session.get(f"{self._api_base}{path}", params=params, timeout=req_timeout) as resp:
                if resp.status != 200:
                    try:
                        err = (await resp.json(content_type=None)).get("error", "")
                    except Exception:
                        err = (await resp.text())[:120]
                    raise RuntimeError(f"接口 HTTP {resp.status} {err}".strip())
                return await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise RuntimeError(f"网络请求失败: {e}") from e

    async def _api_search(self, name: str) -> list[dict]:
        """调用站点 /api/search（服务端已带本地兜底，上游 404 也能命中库内玩家）。

        原先插件直连 gametools 搜索再逐个查档案，最坏要 1+N 次请求；
        现在一次请求拿到候选，后续只对命中项查一次档案。
        """
        try:
            data = await self._api_get("/api/search", {"query": name},
                                       timeout_s=max(60, int(self.config.get("search_timeout", 1000))))
            items = data.get("results") if isinstance(data, dict) else None
            return items if isinstance(items, list) else []
        except Exception as e:
            logger.warning(f"[bf6] 站点搜索失败: {e}")
            return []

    def _template(self, name: str) -> str:
        """带缓存的模板读取（模板不会在运行时变更）"""
        tpl = self._tpl_cache.get(name)
        if tpl is None:
            tpl = (TPL_DIR / name).read_text(encoding="utf-8")
            self._tpl_cache[name] = tpl
        return tpl

    async def terminate(self):
        """插件卸载/重载时关闭共享会话"""
        if self._http and not self._http.closed:
            await self._http.close()
        self._http = None

    async def _gt_player_search(self, name: str) -> list[tuple[str, str, str]]:
        """GameTools /bf6/player/ 跨平台搜人 -> [(displayName, platform, nucleusId)]

        冷门名字上游需现查 EA 后端，耗时可能较长，超时单独放宽。
        """
        timeout = aiohttp.ClientTimeout(total=max(60, int(self.config.get("search_timeout", 1000))))
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(GT_PLAYER_SEARCH,
                                       params={"name": name, "limit": 100}) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            logger.warning("[bf6] GameTools 搜人超时")
            return []
        except aiohttp.ClientError as e:
            logger.warning(f"[bf6] GameTools 搜人失败: {e}")
            return []
        results = data.get("results") if isinstance(data, dict) else data
        if not isinstance(results, list):
            return []
        out: list[tuple[str, str, str]] = []
        for o in results:
            if not isinstance(o, dict):
                continue
            disp = str(o.get("displayName") or o.get("username") or "").strip()
            plat = str(o.get("platform") or o.get("platformId") or "").strip().lower()
            nid = str(o.get("nucleusId") or o.get("userId") or o.get("user_id") or "").strip()
            if not disp or not nid:
                continue
            if plat == "ea":
                plat = "origin"
            if plat not in PLATFORM_ZH:
                continue
            out.append((disp, plat, nid))
        # 精确同名优先
        out.sort(key=lambda c: c[0].lower() != name.strip().lower())
        return out

    def _remember_resolve(self, key: str, ident: str, plat: str):
        if len(self._resolve_cache) > 300:
            self._resolve_cache.pop(next(iter(self._resolve_cache)), None)
        self._resolve_cache[key] = (ident, plat)

    def _profile_parts(self, profile: dict, fallback_name: str, fallback_plat: str = ""):
        pi = (profile.get("data", {}) or {}).get("platformInfo", {}) or {}
        ident = str(pi.get("platformUserIdentifier") or "")
        handle = str(pi.get("platformUserHandle") or fallback_name)
        slug = str(pi.get("platformSlug", "") or fallback_plat)
        return ident, handle, slug, PLATFORM_ZH.get(slug, slug.upper())

    async def _resolve_player(self, name: str):
        """按名称解析玩家 -> (profile, identifier, handle, platform_label)

        顺序: 本地解析缓存 -> 站点 /api/search（服务端已带本地兜底）
              -> 站点按名直接查档案
        每次解析最多 2 次请求，且已追踪玩家在第 1 步就能命中。
        """
        key = name.strip().lower()

        # 1) 本地缓存命中: 直接按 ID 取档案（1 次请求）
        hit = self._resolve_cache.get(key)
        if hit:
            ident, plat = hit
            try:
                profile = await self._api_get("/api/profile",
                                              {"identifier": ident, "platform": plat})
                rid, handle, slug, label = self._profile_parts(profile, name, plat)
                if rid:
                    return profile, rid, handle, label
            except Exception as e:
                logger.warning(f"[bf6] 缓存档案拉取失败，走完整解析: {e}")

        # 2) 站点搜索（含本地已追踪玩家兜底）
        cands = await self._api_search(name)
        if not cands:
            # 3) 兜底: 站点按名跨平台探测
            profile = await self._api_get("/api/profile", {"query": name})
            ident, handle, slug, label = self._profile_parts(profile, name)
            if not ident:
                raise RuntimeError(f"未找到玩家「{name}」")
            self._remember_resolve(key, ident, slug)
            return profile, ident, handle, label

        def _rank(c: dict) -> tuple:
            disp = str(c.get("displayName") or "")
            return (disp.strip().lower() != key, not c.get("tracked"))

        last_err: Exception | None = None
        for c in sorted(cands, key=_rank):
            nid = str(c.get("nucleusId") or "").strip()
            plat = str(c.get("platform") or c.get("platformId") or "").strip().lower()
            if plat == "ea":
                plat = "origin"
            if not nid:
                continue
            try:
                profile = await self._api_get("/api/profile",
                                              {"identifier": nid, "platform": plat or "origin"})
                ident, handle, slug, label = self._profile_parts(
                    profile, str(c.get("displayName") or name), plat)
                if not ident:
                    continue
                self._remember_resolve(key, ident, slug or plat)
                return profile, ident, handle, label
            except Exception as e:
                last_err = e
                logger.warning(f"[bf6] 按 {nid}@{plat} 拉取档案失败: {e}")
                continue

        raise RuntimeError(f"未找到玩家「{name}」") from last_err

    async def _render_image(self, tpl_name: str, data: dict) -> str:
        """读取模板文件 -> 注入主题 -> AstrBot 内置 html_render 出图。

        return_url=False: 返回本地文件路径而非 URL。协议端(NapCat 等)
        发送本地文件时无需自行下载图片，避免其访问不了渲染服务地址
        导致 sendMsg 超时(retcode 1200)。
        """
        data = dict(data)
        data.setdefault("theme", str(self.config.get("theme", "dark")))
        tmpl = self._template(tpl_name)
        # T2I 服务的 page.goto 默认超时只有 30 秒。总览图高 6000+ px、含约 40 张
        # 上游 CDN 图片，在低配服务器上首屏 load 经常超过 30 秒，会直接抛
        # "All endpoints failed: HTTP 500"。这里显式放宽到 150 秒。
        options = {
            "full_page": True,
            "type": "jpeg",
            "quality": 90,
            "timeout": int(self.config.get("render_timeout", 150000)),
        }
        return await self.html_render(tmpl, data, return_url=False, options=options)

    def _remember_session(self, umo: str, info: dict):
        if len(self._sessions) > 300:
            self._sessions.pop(next(iter(self._sessions)), None)
        self._sessions[umo] = info

    @staticmethod
    def _parse_page(page_str: str, current: int) -> int:
        if page_str in PAGE_WORDS_NEXT:
            return current + 1
        if page_str in PAGE_WORDS_PREV:
            return max(1, current - 1)
        try:
            return max(1, int(page_str))
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _unwrap_event(ev):
        """兼容不同 AstrBot 版本的 LLM 工具第一个参数。

        稳定版直接传入 AstrMessageEvent；v4.26 部分 beta 版传入
        ContextWrapper（真实 event 在 .context.event 上）。
        """
        if hasattr(ev, "plain_result") and hasattr(ev, "image_result"):
            return ev
        seen = set()
        cur = ev
        for _ in range(4):
            if id(cur) in seen:
                break
            seen.add(id(cur))
            for attr in ("context", "event", "agent_context"):
                nxt = getattr(cur, attr, None)
                if nxt is None or id(nxt) in seen:
                    continue
                if hasattr(nxt, "plain_result") and hasattr(nxt, "image_result"):
                    return nxt
                inner = getattr(nxt, "event", None)
                if inner is not None and hasattr(inner, "plain_result") and hasattr(inner, "image_result"):
                    return inner
                cur = nxt
                break
        return ev

    # ---------- 核心流程 ----------

    async def _do_overview(self, player_name: str):
        """总览: 返回 (图片URL, 文字摘要)"""
        profile, ident, handle, plat = await self._resolve_player(player_name)
        data = build_overview(profile, top_n=max(3, min(15, int(self.config.get("top_n", 8)))))
        url = await self._render_image("overview.html", data)
        ov = next(s for s in profile["data"]["segments"] if s["type"] == "overview")
        st = ov["stats"]
        gv = lambda k: st.get(k, {}).get("displayValue", "-")
        summary = (f"{handle}({plat}) 生涯战绩: K/D {gv('kd')}，击杀 {gv('kills')}，"
                   f"KPM {gv('killsPerMinute')}，SPM {gv('scorePerMinute')}，"
                   f"胜率 {gv('wl')}，时长 {gv('timePlayed')}")
        return url, summary

    async def _do_reports(self, event: AstrMessageEvent, player_name: str, page: int):
        """战报: 返回 (图片URL, 文字摘要, 总场数, 当前页)"""
        sess = self._sessions.get(event.unified_msg_origin)
        if not player_name and sess:
            player_name = sess["name"]

        profile, ident, handle, plat = await self._resolve_player(player_name)
        page_size = max(1, min(20, int(self.config.get("page_size", 5))))
        md = await self._api_get("/api/matches", {
            "identifier": ident, "limit": page_size, "offset": (page - 1) * page_size,
        })
        matches = md.get("matches", []) or []
        total = int(_n(md.get("totalCount")))
        if not matches:
            raise RuntimeError(f"暂无更多战报(第 {page} 页)")

        data = build_matches(matches, {"name": handle, "platform_label": plat})
        total_pages = max(1, math.ceil(total / page_size)) if total else page
        data["page"], data["total_pages"] = page, total_pages
        url = await self._render_image("matches.html", data)

        self._remember_session(event.unified_msg_origin, {
            "identifier": ident, "name": handle, "platform_label": plat, "page": page,
        })
        brief = "；".join(
            f"{m['result_label']} 击杀{m['stats'][0]['v']} 死亡{m['stats'][1]['v']}"
            for m in data["matches"])
        summary = f"{handle} 最新战报(第{page}/{total_pages}页，共{total}场): {brief}"
        return url, summary, total, page

    # ==================== 指令 ====================

    @filter.command("bf6")
    async def cmd_stats(self, event: AstrMessageEvent, player_name: str = ""):
        """查询战地6玩家生涯战绩总览图。用法: /bf6 玩家名"""
        if not player_name:
            yield event.plain_result("用法: /bf6 <玩家名>")
            event.stop_event()
            return
        try:
            url, _ = await self._do_overview(player_name)
            yield event.image_result(url)
        except Exception as e:
            logger.error(f"[bf6] 总览查询失败: {e}")
            yield event.plain_result(f"查询失败：{e}")
        finally:
            # 阻止该消息继续进入 LLM/Agent 阶段，避免智能体把指令文本再查询一遍
            event.stop_event()

    @filter.command("bf6战报")
    async def cmd_reports(self, event: AstrMessageEvent, player_name: str = "", page: str = "1"):
        """查询战地6玩家最新对局战报图(分页)。用法: /bf6战报 玩家名 [页码|下一页|上一页]"""
        sess = self._sessions.get(event.unified_msg_origin)
        # "/bf6战报 下一页" —— 名字位置直接出现翻页词时使用会话记录
        if player_name in PAGE_WORDS_NEXT + PAGE_WORDS_PREV:
            page, player_name = player_name, ""
        if not player_name and not sess:
            yield event.plain_result("用法: /bf6战报 <玩家名> [页码|下一页|上一页]")
            event.stop_event()
            return
        cur = sess["page"] if sess else 1
        page_no = self._parse_page(page, cur)
        try:
            url, _, _, _ = await self._do_reports(event, player_name, page_no)
            yield event.image_result(url)
        except Exception as e:
            logger.error(f"[bf6] 战报查询失败: {e}")
            yield event.plain_result(f"查询失败：{e}")
        finally:
            # 阻止该消息继续进入 LLM/Agent 阶段，避免智能体把指令文本再查询一遍
            event.stop_event()

    @filter.command("bf6下一页")
    async def cmd_next_page(self, event: AstrMessageEvent):
        """查看上一次战报查询的下一页。用法: /bf6下一页"""
        sess = self._sessions.get(event.unified_msg_origin)
        if not sess:
            yield event.plain_result("请先使用 /bf6战报 <玩家名> 查询一次战报")
            event.stop_event()
            return
        try:
            url, _, _, _ = await self._do_reports(event, "", sess["page"] + 1)
            yield event.image_result(url)
        except Exception as e:
            logger.error(f"[bf6] 下一页失败: {e}")
            yield event.plain_result(f"查询失败：{e}")
        finally:
            # 阻止该消息继续进入 LLM/Agent 阶段，避免智能体把指令文本再查询一遍
            event.stop_event()

    @filter.command("bf6上一页")
    async def cmd_prev_page(self, event: AstrMessageEvent):
        """查看上一次战报查询的上一页。用法: /bf6上一页"""
        sess = self._sessions.get(event.unified_msg_origin)
        if not sess:
            yield event.plain_result("请先使用 /bf6战报 <玩家名> 查询一次战报")
            event.stop_event()
            return
        try:
            url, _, _, _ = await self._do_reports(event, "", max(1, sess["page"] - 1))
            yield event.image_result(url)
        except Exception as e:
            logger.error(f"[bf6] 上一页失败: {e}")
            yield event.plain_result(f"查询失败：{e}")
        finally:
            # 阻止该消息继续进入 LLM/Agent 阶段，避免智能体把指令文本再查询一遍
            event.stop_event()

    # ==================== 智能体(LLM 工具) ====================
    # 注意：LLM 工具的返回值中只有最后一个会被框架处理，因此这里用
    # await event.send() 立即把图片发给用户，再 return 文本摘要给大模型。

    @filter.llm_tool(name="bf6_stats")
    async def tool_stats(self, event, player_name: str):
        """查询战地6(Battlefield 6)玩家的生涯战绩总览，并向当前会话发送一张战绩总览图片。

        Args:
            player_name(string): 玩家名称(EA/Steam/PSN/Xbox 昵称或数字ID)
        """
        try:
            event = self._unwrap_event(event)
            url, summary = await self._do_overview(player_name)
            await event.send(event.image_result(url))
            return summary
        except Exception as e:
            logger.error(f"[bf6] 工具总览查询失败: {e}")
            return f"查询失败：{e}"

    @filter.llm_tool(name="bf6_battle_reports")
    async def tool_reports(self, event, player_name: str, page: int = 1):
        """查询战地6(Battlefield 6)玩家的最新对局战报，并向当前会话发送战报图片。

        Args:
            player_name(string): 玩家名称(EA/Steam/PSN/Xbox 昵称或数字ID)
            page(number): 页码，从 1 开始，每页展示若干场对局，默认第 1 页
        """
        try:
            event = self._unwrap_event(event)
            url, summary, _, _ = await self._do_reports(
                event, player_name, self._parse_page(str(page), 1))
            await event.send(event.image_result(url))
            return summary
        except Exception as e:
            logger.error(f"[bf6] 工具战报查询失败: {e}")
            return f"查询失败：{e}"

    async def terminate(self):
        """插件卸载/停用"""
        self._sessions.clear()
