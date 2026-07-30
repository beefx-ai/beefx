from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from bfxps_customer_bridge import (
    SessionSnapshot,
    _engine_display,
    _fmt,
    _plan_key,
    build_context,
    evaluate_live_plan,
    llm_payload,
)

ADVISOR_VERSION = "BFXPS-SMART-ADVISOR-8.35-PYTHON-DYNAMIC-CHARTS-30D-R5"

def question_complexity(question: str) -> dict[str, Any]:
    q = " ".join(question.lower().split())
    dimensions = []
    groups = {
        "price": ["giá", "open", "mở cửa", "entry", "target", "chốt"],
        "action": ["làm gì", "nên", "vào", "đứng ngoài", "long", "short"],
        "risk": ["rủi ro", "stop", "cắt lỗ", "đuổi", "size", "units"],
        "reason": ["tại sao", "vì sao", "giải thích", "logic"],
        "evidence": ["backtest", "bằng chứng", "lịch sử", "hiệu quả", "win rate"],
        "scenario": ["nếu", "giả sử", "trường hợp", "kịch bản", "gap"],
    }
    for name, words in groups.items():
        if any(w in q for w in words):
            dimensions.append(name)
    score = len(dimensions) + (1 if len(q) > 120 else 0) + (1 if q.count("?") > 1 else 0)
    return {"dimensions": dimensions, "score": score, "compound": score >= 3}



@dataclass
class Turn:
    question: str
    answer: str
    intent: str
    as_of: str
    focus_engine: str = ""
    focus_horizon: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class ConversationMemory:
    session_id: str = "default"
    focus_engine: str = ""
    focus_horizon: str = ""
    focus_date: str = ""
    focus_target_kind: str = ""
    last_live_price: float | None = None
    last_snapshot: dict[str, float | None] = field(default_factory=dict)
    last_context_fingerprint: str = ""
    answer_style: str = "focused"
    tactical_side: str = ""
    tactical_mode: str = ""
    tactical_entry: float | None = None
    position_status: str = ""
    last_snapshot_source: str = ""
    last_market_update_question: str = ""
    turns: list[Turn] = field(default_factory=list)

    def trim(self, max_turns: int = 30) -> None:
        if len(self.turns) > max_turns:
            self.turns = self.turns[-max_turns:]

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["turns"] = [asdict(t) for t in self.turns]
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationMemory":
        turns = [Turn(**x) for x in data.get("turns", [])]
        return cls(
            session_id=data.get("session_id", "default"),
            focus_engine=data.get("focus_engine", ""),
            focus_horizon=data.get("focus_horizon", ""),
            focus_date=data.get("focus_date", ""),
            focus_target_kind=data.get("focus_target_kind", ""),
            last_live_price=data.get("last_live_price"),
            last_snapshot=data.get("last_snapshot", {}),
            last_context_fingerprint=data.get("last_context_fingerprint", ""),
            answer_style=data.get("answer_style", "focused"),
            tactical_side=data.get("tactical_side", ""),
            tactical_mode=data.get("tactical_mode", ""),
            tactical_entry=data.get("tactical_entry"),
            position_status=data.get("position_status", ""),
            last_snapshot_source=data.get("last_snapshot_source", ""),
            last_market_update_question=data.get("last_market_update_question", ""),
            turns=turns,
        )


@dataclass
class AdvisorReply:
    text: str
    intent: str
    context: dict[str, Any]
    focus_plan: dict[str, Any] | None
    structured: dict[str, Any]


class MemoryStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None

    def load(self, session_id: str) -> ConversationMemory:
        if not self.path or not self.path.exists():
            return ConversationMemory(session_id=session_id)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            sessions = data.get("sessions", {})
            return ConversationMemory.from_dict(sessions.get(session_id, {"session_id": session_id}))
        except Exception:
            return ConversationMemory(session_id=session_id)

    def save(self, memory: ConversationMemory) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {"sessions": {}}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                data = {"sessions": {}}
        data.setdefault("sessions", {})[memory.session_id] = memory.to_dict()
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


class SmartAdvisor:
    def __init__(
        self,
        trades_path: str | Path,
        ohlc_path: str | Path | None = None,
        history_trades_path: str | Path | None = None,
        warning_catalog_path: str | Path | None = None,
        memory_path: str | Path | None = None,
        policy_path: str | Path | None = None,
    ):
        self.trades_path = Path(trades_path)
        self.ohlc_path = Path(ohlc_path) if ohlc_path else None
        self.history_trades_path = Path(history_trades_path) if history_trades_path else self.trades_path
        self.warning_catalog_path = Path(warning_catalog_path) if warning_catalog_path else None
        self.memory_store = MemoryStore(memory_path)
        self.policy = self._load_policy(policy_path)

    @staticmethod
    def _load_policy(path: str | Path | None) -> dict[str, Any]:
        if not path:
            path = Path(__file__).resolve().parents[1] / "config" / "advisor_policy.json"
        p = Path(path)
        if not p.exists():
            return {"memory": {"max_turns": 30}, "guardrails": []}
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().strip())

    @staticmethod
    def _fold(text: str) -> str:
        """Accent-insensitive, punctuation-light text for entity routing."""
        raw = unicodedata.normalize("NFKD", str(text).lower())
        raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
        raw = raw.replace("đ", "d")
        return re.sub(r"[^a-z0-9+._]+", " ", raw).strip()

    @staticmethod
    def _parse_numbers(text: str) -> list[float]:
        raw = re.findall(r"(?<!\d)(\d{1,4}(?:[.,]\d+)?)(?!\d)", text)
        values: list[float] = []
        for token in raw:
            s = token.strip()
            if "." in s and "," in s:
                if s.rfind(",") > s.rfind("."):
                    s = s.replace(".", "").replace(",", ".")
                else:
                    s = s.replace(",", "")
            elif "," in s:
                s = s.replace(",", ".")
            elif s.count(".") == 1:
                left, right = s.split(".")
                if len(right) == 3 and len(left) <= 2:
                    s = left + right
            try:
                values.append(float(s))
            except ValueError:
                pass
        return values

    @staticmethod
    def _extract_date(text: str) -> str | None:
        m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", text)
        if not m:
            return None
        day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if year is None:
            year = "2026"
        if len(year) == 2:
            year = "20" + year
        return f"{day:02d}/{month:02d}/{int(year):04d}"

    @staticmethod
    def _parse_market_token(token: str) -> float | None:
        """Parse 1,807.8 / 1.807,8 / 1807,8 / 1,807 without confusing volume/date."""
        raw = str(token).strip().replace(" ", "")
        if not raw or not re.fullmatch(r"\d+(?:[.,]\d+)*", raw):
            return None
        if "." in raw and "," in raw:
            decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
            thousands = "." if decimal == "," else ","
            normalized = raw.replace(thousands, "").replace(decimal, ".")
        elif "," in raw or "." in raw:
            sep = "," if "," in raw else "."
            parts = raw.split(sep)
            if len(parts) > 2:
                # Repeated separators are thousands groups unless the final group is 1-2 decimals.
                if len(parts[-1]) <= 2:
                    normalized = "".join(parts[:-1]) + "." + parts[-1]
                else:
                    normalized = "".join(parts)
            else:
                left, right = parts
                if len(right) == 3 and len(left) <= 3:
                    normalized = left + right
                else:
                    normalized = left + "." + right
        else:
            normalized = raw
        try:
            return float(normalized)
        except ValueError:
            return None

    @classmethod
    def _extract_tabular_ohlc(cls, text: str) -> dict[str, Any] | None:
        """Read pasted market rows: Date | Open | Close | High | Low | Volume | OI | Change.

        The old generic number fallback could mistake the year (2026), volume or OI for price.
        This parser only accepts the first four post-date price columns and validates OHLC geometry.
        """
        folded = cls._fold(text)
        header_hits = sum(term in folded for term in (
            "mo cua", "dong cua", "cao nhat", "thap nhat",
            "open", "close", "high", "low",
        ))
        has_table_header = header_hits >= 3
        token_re = re.compile(r"(?<![\d/])(?:\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d{1,7}(?:[.,]\d+)?)(?![\d/])")
        date_re = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")
        for dm in reversed(list(date_re.finditer(text))):
            values: list[float] = []
            for tok in token_re.findall(text[dm.end():]):
                val = cls._parse_market_token(tok)
                if val is not None:
                    values.append(val)
            if len(values) < 4:
                continue
            open_p, close_p, high_p, low_p = values[:4]
            prices = (open_p, close_p, high_p, low_p)
            if not all(500.0 <= x <= 5000.0 for x in prices):
                continue
            if high_p + 1e-9 < max(open_p, close_p, low_p):
                continue
            if low_p - 1e-9 > min(open_p, close_p, high_p):
                continue
            day, month, year = int(dm.group(1)), int(dm.group(2)), dm.group(3)
            if len(year) == 2:
                year = "20" + year
            return {
                "date": f"{day:02d}/{month:02d}/{int(year):04d}",
                "open": open_p,
                "close": close_p,
                "high": high_p,
                "low": low_p,
                "is_completed_bar": bool(has_table_header and ("dong cua" in folded or "close" in folded)),
                "source": "header_table" if has_table_header else "date_row",
            }
        return None

    @staticmethod
    def _detect_answer_style(text: str) -> tuple[str | None, bool]:
        q = SmartAdvisor._norm(text)
        short_terms = [
            "nói ngắn", "noi ngan", "trả lời ngắn", "tra loi ngan", "ngắn hơn", "ngan hon", "ngắn thôi", "ngan thoi", "ngắn gọn", "ngan gon", "súc tích", "suc tich",
            "rút gọn", "rut gon", "tóm tắt", "tom tat", "chỉ kết luận", "chi ket luan",
            "1 câu", "một câu", "2 câu", "hai câu", "đừng dài", "dung dai",
        ]
        detail_terms = [
            "nói kỹ", "noi ky", "chi tiết", "chi tiet", "đầy đủ", "day du",
            "phân tích sâu", "phan tich sau", "nói dài", "noi dai", "giải thích kỹ", "giai thich ky",
        ]
        style: str | None = None
        matched: list[str] = []
        for term in short_terms:
            if term in q:
                style = "short"
                matched.append(term)
        for term in detail_terms:
            if term in q:
                style = "detailed"
                matched.append(term)
        if style is None:
            return None, False
        residue = q
        for term in matched:
            residue = residue.replace(term, " ")
        residue = re.sub(r"[^a-z0-9à-ỹđ]+", " ", residue, flags=re.IGNORECASE)
        residue = re.sub(r"\s+", " ", residue).strip()
        pure = residue in {"", "thôi", "nhe", "nhé", "đi", "lại", "lại đi"}
        return style, pure

    @staticmethod
    def _extract_labeled_price(text: str, patterns: list[str]) -> float | None:
        for pattern in patterns:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if not m:
                continue
            values = SmartAdvisor._parse_numbers(m.group(1))
            if values and values[0] >= 500:
                return values[0]
        return None

    @staticmethod
    def _extract_explicit_open(text: str) -> float | None:
        return SmartAdvisor._extract_labeled_price(text, [
            r"(?:giá\s*)?(?:mở\s*cửa|mo\s*cua|open|ato|phiên\s*mở|phien\s*mo|mở|mo)\s*(?:là|la|=|:)?\s*(\d{3,4}(?:[.,]\d+)?)",
            r"(?:^|[\s,;/])o\s*(?:=|:)?\s*(\d{3,4}(?:[.,]\d+)?)",
        ])

    @staticmethod
    def _extract_explicit_live_price(text: str) -> float | None:
        return SmartAdvisor._extract_labeled_price(text, [
            r"(?:giá\s*)?(?:hiện\s*tại|hien\s*tai|live|last\s*price|current\s*price)\s*(?:là|la|=|:)?\s*(\d{3,4}(?:[.,]\d+)?)",
            r"giá\s*(?:đang|dang)\s*(?:là|la|=|:)?\s*(\d{3,4}(?:[.,]\d+)?)",
            r"(?:^|[\s,;/])giá\s*(?:là|la|=|:)?\s*(\d{3,4}(?:[.,]\d+)?)",
            r"(?:bây\s*giờ|bay\s*gio|giờ|gio|hiện|hien)\s*(?:là|la|=|:)?\s*(\d{3,4}(?:[.,]\d+)?)",
            r"(?:^|[\s,;/])(?:p|last|c)\s*(?:=|:)?\s*(\d{3,4}(?:[.,]\d+)?)",
        ])

    @staticmethod
    def _extract_safe_standalone_live_price(text: str) -> float | None:
        """Accept a new live tick only when the utterance clearly reports market price.

        Plan references such as "chờ 1835", "entry 1835" or "sao không khớp 1835"
        are deliberately rejected. This prevents conversational numbers from rewriting OHLC.
        """
        q = SmartAdvisor._fold(text)
        patterns = [
            r"^(?:p|price|gia|last)?\s*(\d{3,4}(?:[.,]\d+)?)\s*(?:roi|bay gio|gio)?$",
            r"^(?:gio|bay gio|hien tai|luc nay)\s*(?:gia\s*)?(?:la|o|dang o)?\s*(\d{3,4}(?:[.,]\d+)?)\s*(?:roi)?$",
            r"^(?:gia\s*)?(?:len|xuong|ve|dang o|dang tai)\s*(\d{3,4}(?:[.,]\d+)?)\s*(?:roi)?(?:\s*,?\s*(?:gio|thi|lam gi|sao))?$",
        ]
        blocked = [
            "cho ", "entry", "target", "moc", "vung", "khop", "fill", "lenh",
            "short ", "long ", "tai ", "tu ", "bao ", "doi ", "canh ",
        ]
        if any(x in q for x in blocked) and not q.startswith(("gio ", "bay gio ", "hien tai ", "gia len ", "gia xuong ", "gia ve ", "gia dang ")):
            return None
        for pat in patterns:
            m = re.fullmatch(pat, q, flags=re.IGNORECASE)
            if m:
                vals = SmartAdvisor._parse_numbers(m.group(1))
                if vals and 500 <= vals[0] <= 5000:
                    return vals[0]
        return None

    @staticmethod
    def _has_explicit_market_update(text: str, tabular_ohlc: dict[str, Any] | None = None) -> bool:
        if tabular_ohlc:
            return True
        return any(x is not None for x in (
            SmartAdvisor._extract_explicit_open(text),
            SmartAdvisor._extract_explicit_live_price(text),
            SmartAdvisor._extract_explicit_high(text),
            SmartAdvisor._extract_explicit_low(text),
            SmartAdvisor._extract_safe_standalone_live_price(text),
        ))

    @staticmethod
    def _extract_explicit_high(text: str) -> float | None:
        return SmartAdvisor._extract_labeled_price(text, [
            r"(?:giá\s*)?(?:cao\s*nhất|cao\s*nhat|high|đỉnh\s*phiên|dinh\s*phien|cao)\s*(?:là|la|=|:)?\s*(\d{3,4}(?:[.,]\d+)?)",
            r"(?:^|[\s,;/])h\s*(?:=|:)?\s*(\d{3,4}(?:[.,]\d+)?)",
        ])

    @staticmethod
    def _extract_explicit_low(text: str) -> float | None:
        return SmartAdvisor._extract_labeled_price(text, [
            r"(?:giá\s*)?(?:thấp\s*nhất|thap\s*nhat|low|đáy\s*phiên|day\s*phien|thấp|thap)\s*(?:là|la|=|:)?\s*(\d{3,4}(?:[.,]\d+)?)",
            r"(?:^|[\s,;/])l\s*(?:=|:)?\s*(\d{3,4}(?:[.,]\d+)?)",
        ])

    @staticmethod
    def _detect_requested_side(text: str) -> str | None:
        q = SmartAdvisor._norm(text)
        long_terms = [
            "muốn long", "nhất định long", "vẫn long", "đánh long", "mua lên",
            "long lên", "canh long", "kèo long", "long thì sao", "long thi sao",
            "nếu long", "neu long", "cho tao long", "cho tôi long",
        ]
        short_terms = [
            "muốn short", "nhất định short", "vẫn short", "đánh short", "bán xuống",
            "short xuống", "canh short", "kèo short", "short thì sao", "short thi sao",
            "nếu short", "neu short", "cho tao short", "cho tôi short",
        ]
        long_score = sum(2 for k in long_terms if k in q)
        short_score = sum(2 for k in short_terms if k in q)
        if re.search(r"\blong\b", q):
            long_score += 1
        if re.search(r"\bshort\b", q):
            short_score += 1

        # Câu phủ định một phía thường hàm ý khách muốn phía còn lại.
        if re.search(r"(?:không|ko|khong|chẳng|chả)\s+(?:muốn\s+)?short", q) or "không thích short" in q or "ko thích short" in q:
            long_score += 4
        if re.search(r"(?:không|ko|khong|chẳng|chả)\s+(?:muốn\s+)?long", q) or "không thích long" in q or "ko thích long" in q:
            short_score += 4

        # Câu cực ngắn vẫn là yêu cầu đánh giá một phía, không phải từ khóa rác.
        if re.fullmatch(r"(?:thế\s+)?long(?:\s+thì\s+sao|\s+thi\s+sao|\s+được\s+không|\s+duoc\s+khong)?[?.!]*", q):
            long_score += 5
        if re.fullmatch(r"(?:thế\s+)?short(?:\s+thì\s+sao|\s+thi\s+sao|\s+được\s+không|\s+duoc\s+khong)?[?.!]*", q):
            short_score += 5
        if long_score > short_score:
            return "LONG"
        if short_score > long_score:
            return "SHORT"
        return None

    @staticmethod
    def _is_countertrend_request(question: str) -> bool:
        q = SmartAdvisor._norm(question)
        requested = SmartAdvisor._detect_requested_side(q)
        insist = any(k in q for k in [
            "nhất định", "vẫn muốn", "cứ muốn", "buộc phải", "muốn đánh",
            "muốn long", "muốn short", "không thích short", "ko thích short",
            "không thích long", "ko thích long", "kèo ngược hệ", "ngược hệ",
        ])
        opposing_zone = any(k in q for k in [
            "đến điểm", "tới điểm", "lên vùng", "xuống vùng", "vùng hệ",
            "hệ báo chờ", "entry short", "entry long", "chờ short", "chờ long",
        ])
        return bool(requested and (insist or opposing_zone))

    @staticmethod
    def _last_substantive_question(memory: ConversationMemory) -> str | None:
        for turn in reversed(memory.turns):
            style, pure = SmartAdvisor._detect_answer_style(turn.question)
            if not (style and pure):
                return turn.question
        return None

    @staticmethod
    def _is_accountability_request(question: str) -> bool:
        q = SmartAdvisor._norm(question)
        terms = [
            "mày sai", "may sai", "sai rồi", "sai roi", "hệ sai", "he sai",
            "dự báo sai", "du bao sai", "tại mày", "tai may", "tại hệ", "tai he",
            "mày bảo", "may bao", "bảo short", "bao short", "bảo long", "bao long",
            "đang lỗ", "dang lo", "kẹt lệnh", "ket lenh", "kẹt short", "kẹt long",
            "cứu lệnh", "cuu lenh", "xử lý đi", "xu ly di", "thua rồi", "thua roi",
            "cắt rồi", "cat roi", "vừa cắt", "vua cat", "gỡ", "go lo", "gỡ lỗ", "go lo",
            "trách", "đền", "chịu trách nhiệm", "chiu trach nhiem",
        ]
        return any(x in q for x in terms)

    @staticmethod
    def _is_hypothetical_condition(question: str) -> bool:
        q = SmartAdvisor._norm(question)
        starters = ("nếu ", "neu ", "giả sử ", "gia su ", "khi ", "trường hợp ", "truong hop ")
        condition_terms = [
            "nếu giá", "neu gia", "nếu lên", "neu len", "nếu xuống", "neu xuong",
            "nếu rơi", "neu roi", "nếu vượt", "neu vuot", "nếu thủng", "neu thung",
            "nếu giữ", "neu giu", "rơi lại", "roi lai", "mất lại", "mat lai",
            "giá vượt", "gia vuot", "đã vượt", "da vuot", "vượt mạnh", "vuot manh",
            "giá thủng", "gia thung", "đã thủng", "da thung", "giữ trên", "giu tren", "giữ dưới", "giu duoi",
        ]
        return q.startswith(starters) or any(x in q for x in condition_terms)

    @staticmethod
    def _extract_position_claim(question: str) -> tuple[str | None, float | None]:
        q = SmartAdvisor._norm(question)
        # Chỉ nhận là vị thế thật khi người dùng nói rõ đã/đang/vào/kẹt lệnh; không
        # coi câu "short thì sao" hay "mày bảo short 1835" là đã có vị thế.
        patterns = [
            r"(?:tao|tôi|toi|mình|minh)\s+(?:đã\s+|da\s+|dang\s+|đang\s+|vào\s+|vao\s+|kẹt\s+|ket\s+)(long|short)\s*(\d{3,4}(?:[.,]\d+)?)",
            r"(?:tao|tôi|toi|mình|minh)\s+(long|short)\s*(\d{3,4}(?:[.,]\d+)?)(?=.*(?:đang\s+lỗ|dang\s+lo|kẹt|ket|xử\s+lý|xu\s+ly))",
            r"(?:đang|dang|đã|da|vào|vao|kẹt|ket)\s+(long|short)\s*(\d{3,4}(?:[.,]\d+)?)",
            r"(?:lệnh|lenh)\s+(long|short)\s*(?:ở|o|tại|tai)?\s*(\d{3,4}(?:[.,]\d+)?)",
        ]
        for pat in patterns:
            m = re.search(pat, q, flags=re.IGNORECASE)
            if not m:
                continue
            vals = SmartAdvisor._parse_numbers(m.group(2))
            return m.group(1).upper(), (vals[0] if vals else None)
        return None, None

    @staticmethod
    def _classify_intent(question: str) -> str:
        q = SmartAdvisor._norm(question)
        qf = SmartAdvisor._fold(question)
        numeric_values = SmartAdvisor._parse_numbers(q)

        # V8.25 deterministic customer-query router. Explicit information requests
        # beat generic entity/performance matching and conversation memory.
        if any(x in qf for x in ["thieu du lieu", "thieu database", "khong du info", "khong du du lieu", "khong co engine trong base", "thieu expected high", "thieu expected low", "thieu r5 action", "khong du lich su", "du lieu chua cap nhat", "base khong co so lieu", "khong du row", "neu thieu du lieu"]):
            return "MISSING_DATA"
        if any(x in qf for x in ["database dang doc file nao", "nguon database", "file nao cho hieu suat", "file nao cho keo tuong lai", "cho biet nguon du lieu", "data source", "ssot la file nao", "cac database dang dung", "nguon history va forward", "provenance database", "database nao dung"]):
            return "DATA_SOURCE"
        # V8.29 chart-first: presentation requests must beat OHLC scenario parsing.
        chart_plan_phrases_early = [
            "kèo trên chart", "keo tren chart", "đặt kèo lên chart", "dat keo len chart",
            "show kèo trên chart", "show keo tren chart", "chart kèo", "chart keo",
            "biểu đồ kèo", "bieu do keo", "đồ thị kèo", "do thi keo",
            "vẽ kèo", "ve keo", "vẽ chart kèo", "ve chart keo",
            "đặt các kèo lên chart", "dat cac keo len chart",
        ]
        if any(x in qf for x in chart_plan_phrases_early):
            return "PLAN_CHART"
        # V8.30: any chart/diagram request with trade/OHLC content is chart-first
        # unless the customer explicitly asks for an execution decision. This
        # prevents stored OHLC from turning a presentation request into SCENARIO.
        has_chart_word = any(x in qf for x in ["chart", "bieu do", "do thi", "ve nen", "nen ohlc"])
        has_perf_word = any(x in qf for x in ["hieu suat", "performance", "pnl", "equity", "drawdown", "win rate"])
        has_execution_ask = any(x in qf for x in [
            "gio lam gi", "nen long", "nen short", "co nen", "vao lenh", "dat lenh",
            "da fill", "khop lenh", "wait entry", "tang size", "tang khoi luong",
            "binh quan", "them vi the", "cat lo", "stop loss", "sl bao nhieu",
        ])
        chart_trade_word = any(x in qf for x in ["keo", "forecast", "entry", "target", "ohlc", "bien", "center", "expected low", "expected high"]) and "khong noi keo" not in qf
        if has_chart_word and has_perf_word and chart_trade_word and not has_execution_ask:
            return "CHART_COMBINED"
        if has_chart_word and not has_perf_word and not has_execution_ask:
            return "PLAN_CHART"

        # V8.27: performance terms are explicit database queries and must beat
        # remembered OHLC/current-plan routing, including polite/free-form wording.
        perf_core = ["hieu suat", "performance", "pnl", "win rate", "equity", "drawdown", "loi lo", "lai lo"]
        has_wr_token = bool(re.search(r"(?:^|\s)wr(?:$|\s)", qf))
        has_perf_core = any(x in qf for x in perf_core) or has_wr_token
        has_reverse_core = any(x in qf for x in ["danh nguoc", "dao nguoc", "reverse", "opposite", "long thay short", "short thay long", "nguoc he"])
        has_validation_core = any(x in qf for x in ["kiem dinh", "backtest", "audit", "bang chung", "do tin cay", "provenance"])
        has_tomorrow_core = any(x in qf for x in ["keo ngay mai", "ngay mai co keo", "keo phien ke tiep", "tomorrow trade", "tomorrow plan", "keo t+2"])
        if has_perf_core and has_tomorrow_core:
            return "PERFORMANCE_AND_TOMORROW"
        if has_perf_core and has_validation_core:
            return "PERFORMANCE_FULL_AUDIT"
        if has_perf_core and has_reverse_core:
            return "RECENT_PERFORMANCE_REVERSE"
        if has_perf_core:
            return "ENGINE_PERFORMANCE"
        if any(x in qf for x in ["keo hom nay", "hom nay he co", "tach keo tung engine hom nay", "danh sach lenh phien hien hanh", "keo hien tai theo tung engine", "bot dang xet gi hom nay", "plan ngay hom nay", "hom nay entry target", "show keo hom nay", "tong hop keo phien nay"]):
            return "TODAY_PLANS"
        if any(x in qf for x in ["keo ngay mai", "ngay mai co keo", "keo phien ke tiep", "plan tomorrow", "lenh ngay ke tiep", "ngay sau he xet", "keo t+2", "keo 29/7", "keo 29 7", "ngay mai entry target", "show tomorrow trade"]) and any(x in qf for x in ["hieu suat", "pnl", "loi lo", "trades gan nhat", "lenh gan nhat"]):
            return "PERFORMANCE_AND_TOMORROW"
        if any(x in qf for x in ["keo ngay mai", "ngay mai co keo", "keo phien ke tiep", "plan tomorrow", "lenh ngay ke tiep", "ngay sau he xet", "keo t+2", "keo 29/7", "keo 29 7", "ngay mai entry target", "show tomorrow trade"]):
            return "TOMORROW_PLAN"
        if any(x in qf for x in ["keo moi nhat ngay nao", "ngay forward xa nhat", "latest trade date", "date keo moi nhat", "database co keo toi ngay nao", "ngay ke hoach moi nhat", "moc forward cuoi", "keo tuong lai xa nhat", "ngay cuoi trong plan", "cho ngay moi nhat"]):
            return "LATEST_DATE"
        if any(x in qf for x in ["list top engines", "cac engine hien hanh", "3 trades moi engine", "he nao dang chay", "liet ke engine", "top 3 engine", "moi engine ba keo", "recent trades tung engine", "danh sach profile dang co", "show engines active"]):
            return "TOP_ENGINES"
        if any(x in qf for x in ["hieu suat tuan gan day", "hieu suat muoi phien", "hieu suat hai muoi phien", "hieu suat ba muoi trades", "ket qua 30 giao dich gan nhat"]):
            return "RECENT_PERFORMANCE"
        if re.search(r"(?:thong ke|performance)\s+\d{1,3}\s+(phien|sessions|trade|trades|lenh)", qf):
            return "RECENT_PERFORMANCE"
        if re.search(r"ket qua\s+\d{1,3}\s+(ngay|phien|trade|trades|lenh|giao dich)\s+gan nhat", qf):
            return "RECENT_PERFORMANCE"
        if any(x in qf for x in ["lich su", "3 keo gan nhat", "history trades", "cac keo truoc", "giao dich gan day", "last trades", "lich su tung engine", "ba lenh settled gan nhat", "keo cu cua he", "recent history"]) and not any(x in qf for x in ["hieu suat", "pnl", "wr", "loi lo", "lai lo", "ket qua"]):
            return "HISTORY"
        if (re.search(r"(?:^|\s)o\s*[=:]?\s*\d", qf) and re.search(r"(?:^|\s)h\s*[=:]?\s*\d", qf) and re.search(r"(?:^|\s)l\s*[=:]?\s*\d", qf)) or all(x in qf for x in ["open", "high", "low"]):
            return "SCENARIO"
        if any(x in qf for x in ["keo nao fill", "engine nao da khop", "lenh nao wait_entry", "target co qua truoc entry", "kiem tra fill tung engine", "co lenh nao vao duoc", "audit trinh tu entry target", "keo nao chua cham entry", "fill status cac engines", "tong hop khop lenh"]):
            return "ENGINE_FILL_AUDIT"
        if any(x in qf for x in ["du lieu moi toi ngay nao", "outputs co moi khong", "freshness database", "cap nhat gan nhat", "nguon co stale khong", "keo co dung ngay khong", "database forward toi dau", "history cap nhat den ngay nao", "ngay du lieu cuoi", "base co dong bo khong"]):
            return "FRESHNESS"
        if any(x in qf for x in ["muc chung giua cac engines", "consensus levels", "cac nac trung nhau", "engine dong thuan diem nao", "moc 1835 co y nghia", "moc 1844.3 la gi", "uu tien nac chung", "diem giao giua engine5 va simcarrry6", "cac muc dong thuan", "level overlap cua he"]):
            return "CONSENSUS"
        if any(x in qf for x in ["bien tung horizon", "expected high low", "vung forecast cac engine", "khung dao dong theo plan", "range hom nay", "bien t+1 t+2", "cac vung entry target", "bien du bao he", "high low du kien"]):
            return "FORECAST_RANGE"
        if any(x in qf for x in ["di nguoc he", "lam nguoc bot", "reverse pnl", "reverse trades", "danh nguoc tung engine", "doi dau pnl", "long thay short", "short thay long"]):
            return "RECENT_PERFORMANCE_REVERSE"
        if any(x in qf for x in ["nguon pnl la gi", "phan nao chua backtest", "co the tin ket qua", "minh bach bang chung"]):
            return "ADVICE_VALIDATION"
        if ("gap down" in qf and "hoi" in qf and len(numeric_values) >= 2):
            return "SCENARIO"
        if any(x in qf for x in ["high 1837.4 duoc size", "high 1837,4 duoc size", "gap len tren nac cuoi", "open duoi target truoc entry", "gap down", "gap up", "nhay gap", "catch up ladder", "target di qua truoc fill", "gap xuong qua target", "mo cua thap hon muc tieu", "keo chua fill ma gia qua target"]):
            return "LEVEL_EXECUTION"
        if any(x in qf for x in ["long len diem cho short", "mua hoi toi entry short", "sao khong long den diem short", "keo long cau noi", "trigger long reclaim", "tp sl long reclaim", "long nguoc short t+1"]):
            return "BRIDGE_LONG_TO_SHORT"
        if any(x in qf for x in ["short xuong diem cho long", "ban hoi toi entry long", "sao khong short den diem long", "keo short cau noi", "trigger short reclaim", "tp sl short reclaim", "short nguoc long t+1", "advisory short reclaim"]):
            return "BRIDGE_SHORT_TO_LONG"
        if any(x in qf for x in ["units 0 co dao lenh", "r5 co cho long", "r5 action forward", "quyen r5 hien tai"]):
            return "R5_GUIDANCE"
        # Performance transparency queries have priority over trade-memory, engine playbook,
        # date navigation and generic evidence routing. They ask about settled outcomes, not
        # the current position.
        perf_terms = ["pnl", "loi lo", "loi lom", "lai lo", "ket qua", "hieu suat", "hieu qua", "win loss", "wr", "kiem hay mat", "co loi"]
        recent_terms = ["3 ngay", "ba ngay", "3 phien", "ba phien", "gan day", "vua qua", "vua roi", "phien gan nhat", "cac phien gan nhat", "settled gan nhat", "giao dich gan day"]
        reverse_terms = ["danh nguoc", "dao nguoc", "nguoc lai", "reverse", "opposite", "long thay short", "short thay long", "dao direction", "dao long short", "doi dau pnl", "nguoc he", "lam nguoc lai", "dao chieu"]
        validation_terms = ["kiem dinh", "backtest", "audit", "bang chung", "do tin cay", "nguon so lieu", "so lieu nay tu dau", "nguon gi", "provenance", "oos", "tai lap", "promote advisory", "dien giai outputs", "reclaim va ket qua base", "tu van reclaim va ket qua base", "toan ky", "da duoc", "co the tin so pnl"]
        engine_perf_terms = ["tung engine", "theo engine", "tung he", "tung profile", "engine nao loi", "engine nao lo", "so sanh hieu suat engine", "simcarrry6 va engine5", "3 engines", "wr tung engine", "pnl tung engine", "khong cong trung", "dung cong danh muc", "allDaysladder".lower(), "12k va simcarrry6"]
        has_perf = any(x in qf for x in perf_terms)
        has_recent = any(x in qf for x in recent_terms)
        has_reverse = any(x in qf for x in reverse_terms)
        has_validation = any(x in qf for x in validation_terms)
        has_range_perf_conflict = any(x in qf for x in ["bien du kien", "bien", "range", "high low", "high-low", "dao dong"])
        has_engine_perf = (any(x in qf for x in engine_perf_terms) or (("engine" in qf or "profile" in qf) and has_perf)) and not has_range_perf_conflict
        if has_validation and (has_recent or has_reverse or has_engine_perf):
            return "PERFORMANCE_FULL_AUDIT"
        if has_validation:
            return "ADVICE_VALIDATION"
        if has_reverse and (has_perf or has_recent or "keo" in qf or "lenh" in qf or "entry exit" in qf):
            return "RECENT_PERFORMANCE_REVERSE"
        if has_engine_perf:
            return "ENGINE_PERFORMANCE"
        if has_recent and has_perf:
            return "RECENT_PERFORMANCE"
        engine_named = any(x in qf for x in [
            "simcary", "simcarry", "simcarrry", "sim carry", "sim car",
            "simptkt", "sim ptkt", "gpt simptkt", "12k", "ladder", "engine5",
        ])
        engine_detail = any(x in qf for x in [
            "danh cu the", "danh nhu nao", "danh ntn", "cach danh", "keo gi",
            "khoi luong", "volume", "size", "units", "vao ra", "entry target",
        ])
        fill_audit_phrases = [
            "engine nao khop", "engines nao khop", "he nao khop", "lenh nao khop",
            "co engine nao khop", "co lenh nao khop", "tom lai nay co engine",
            "tom lai co engine", "khop duoc lenh", "khop dc lenh", "thuc su khop",
            "ca phien", "ca ngay",
        ]
        asks_fill_audit = any(x in qf for x in fill_audit_phrases) and any(x in qf for x in ["khop", "fill", "vao duoc", "lenh"])
        if asks_fill_audit or ("fill" in qf and "wait_entry" in qf) or ("kèo nào" in qf and "fill" in qf):
            return "ENGINE_FILL_AUDIT"
        if engine_named and any(x in qf for x in ["biên", "bien", "range", "high low", "high-low"]):
            return "FORECAST_RANGE"
        if engine_named and engine_detail:
            return "ENGINE_PLAYBOOK"
        short_system_queries = {
            "keo sao", "keo hom nay sao", "keo hnay sao", "nay keo sao",
            "keo hien tai sao", "cap nhat keo", "cap nhat keo hom nay",
            "hom nay danh sao", "nay danh sao", "he danh sao",
        }
        if qf in short_system_queries or any(x in qf for x in [
            "keo cua he", "cap nhat keo", "update keo",
            "he dang danh gi", "he danh gi", "khoi luong di ntn", "kich ban nao thi dung",
        ]):
            return "SYSTEM_PLAYBOOK"
        # Database navigation intents must beat generic playbook/memory routing.
        history_phrases = [
            "lịch sử", "lich su", "history", "3 kèo gần nhất", "3 keo gan nhat",
            "các kèo gần nhất", "cac keo gan nhat", "kèo trước", "keo truoc",
            "giao dịch gần đây", "giao dich gan day", "recent trades", "last trades",
        ]
        top_engine_phrases = [
            "list top engines", "top engines", "danh sách engine", "danh sach engine",
            "các engine hiện hành", "cac engine hien hanh", "engine hiện hành", "engine hien hanh",
            "hệ nào đang chạy", "he nao dang chay", "những engine nào", "nhung engine nao",
            "liệt kê engine", "liet ke engine", "top 3 engine", "3 engine top",
            "3 trades mỗi engine", "3 trade mỗi engine", "3 kèo mỗi engine", "ba kèo mỗi engine",
            "last 3 trades each engine", "3 trades per engine", "mỗi engine 3 kèo",
            "3 trades moi engine", "3 trade moi engine", "3 keo moi engine", "ba keo moi engine", "moi engine 3 keo",
        ]
        date_phrases = [
            "kh: date", "date", "ngày mới nhất", "ngay moi nhat", "kèo mới nhất ngày",
            "keo moi nhat ngay", "latest date", "latest trade date", "kèo ngày nào", "keo ngay nao",
            "ngày hiện tại của kèo", "ngay hien tai cua keo", "kèo mới nhất", "keo moi nhat",
        ]
        tomorrow_phrases = [
            "kèo ngày mai", "keo ngay mai", "ngày mai có kèo", "ngay mai co keo",
            "tomorrow trade", "tomorrow plan", "kèo phiên kế tiếp", "keo phien ke tiep",
            "kèo kế tiếp", "keo ke tiep",
        ]
        performance_recent_phrases = [
            "3 ngày vừa rồi", "3 ngay vua roi", "ba ngày vừa rồi", "ba ngay vua roi",
            "10 ngày qua", "10 ngay qua", "20 ngày qua", "20 ngay qua", "30 ngày qua", "30 ngay qua",
            "10 phiên qua", "10 phien qua", "20 phiên qua", "20 phien qua", "30 phiên qua", "30 phien qua",
            "10 trades gần nhất", "10 trades gan nhat", "20 trades gần nhất", "20 trades gan nhat", "30 trades gần nhất", "30 trades gan nhat",
            "3 phiên vừa rồi", "3 phien vua roi", "mấy ngày gần đây", "may ngay gan day",
            "lời lõm thế nào", "loi lom the nao", "lãi lỗ thế nào", "lai lo the nao",
            "hiệu suất gần đây", "hieu suat gan day", "pnl gần đây", "pnl gan day",
            "kết quả 3 ngày", "ket qua 3 ngay", "bot giao dịch lời", "bot giao dich loi",
        ]
        reverse_perf_phrases = [
            "đánh ngược", "danh nguoc", "đảo ngược", "dao nguoc", "ngược lại thì", "nguoc lai thi",
            "reverse pnl", "opposite pnl", "nếu long thay short", "neu long thay short",
            "nếu short thay long", "neu short thay long", "đi ngược hệ", "di nguoc he",
        ]
        validation_phrases = [
            "tư vấn của bạn được kiểm định chưa", "tu van cua ban duoc kiem dinh chua",
            "đã kiểm định chưa", "da kiem dinh chua", "được backtest chưa", "duoc backtest chua",
            "có bằng chứng không", "co bang chung khong", "độ tin cậy tư vấn", "do tin cay tu van",
            "số liệu này từ đâu", "so lieu nay tu dau", "kiểm định hiệu suất", "kiem dinh hieu suat",
            "minh bạch hiệu suất", "minh bach hieu suat", "audit tư vấn", "audit tu van",
        ]
        engine_perf_phrases = [
            "hiệu suất từng engine", "hieu suat tung engine", "pnl từng engine", "pnl tung engine",
            "engine nào lời", "engine nao loi", "engine nào lỗ", "engine nao lo",
            "so sánh hiệu suất engine", "so sanh hieu suat engine", "kết quả từng hệ", "ket qua tung he",
        ]
        numeric_perf = bool(re.search(r"(?<!\d)\d{1,3}\s*(ngay|phien|trade|trades|lenh)", qf)) and any(x in qf for x in ["hieu suat", "pnl", "loi", "lo", "giao dich", "trade", "trades", "settled", "du lieu", "history"]) and not any(x in qf for x in top_engine_phrases)
        data_coverage_perf = any(x in qf for x in ["du 30 trades", "bao nhieu row history", "thieu du lieu", "bao phu tu ngay", "co du 30 trade", "co du 20 ngay", "co du 10 ngay", "file dang doc"])
        if any(x in qf for x in validation_phrases):
            return "ADVICE_VALIDATION"
        if numeric_perf or data_coverage_perf:
            return "ENGINE_PERFORMANCE" if any(x in qf for x in ["engine", "engines", "tung he", "tung profile"]) else "RECENT_PERFORMANCE"
        if any(x in qf for x in performance_recent_phrases) and any(x in qf for x in reverse_perf_phrases):
            return "RECENT_PERFORMANCE_REVERSE"
        if any(x in qf for x in engine_perf_phrases):
            return "ENGINE_PERFORMANCE"
        if any(x in qf for x in performance_recent_phrases):
            return "RECENT_PERFORMANCE"
        if any(x in qf for x in reverse_perf_phrases) and any(x in qf for x in ["pnl", "lời", "loi", "lỗ", "lo", "hiệu suất", "hieu suat"]):
            return "RECENT_PERFORMANCE_REVERSE"

        # Chart-only current-plan requests are presentation requests, not execution advice.
        # They must beat remembered OHLC and generic scenario routing.
        chart_plan_phrases = [
            "kèo trên chart", "keo tren chart", "đặt kèo lên chart", "dat keo len chart",
            "show kèo trên chart", "show keo tren chart", "chart kèo", "chart keo",
            "biểu đồ kèo", "bieu do keo", "đồ thị kèo", "do thi keo",
            "vẽ kèo", "ve keo", "vẽ chart kèo", "ve chart keo",
            "đặt các kèo lên chart", "dat cac keo len chart",
        ]
        if any(x in qf for x in chart_plan_phrases):
            return "PLAN_CHART"

        # Explicit database queries must beat conversational memory and generic trade routing.
        if any(x in qf for x in ["bao nhiêu forward", "bao nhieu forward", "forward rows", "số row forward", "so row forward"]):
            return "FORWARD_COUNT"
        if any(x in qf for x in ["database đang đọc file nào", "database dang doc file nao", "nguồn database", "nguon database", "file dữ liệu nào", "file du lieu nao", "database nào dùng", "database nao dung", "database nào dùng để tính hiệu suất", "database nao dung de tinh hieu suat", "database nào dùng cho kèo tương lai", "database nao dung cho keo tuong lai"]):
            return "DATA_SOURCE"
        if any(x in qf for x in ["kèo hôm nay", "keo hom nay", "hôm nay hệ có", "hom nay he co", "tách riêng kèo từng engine hôm nay", "tach rieng keo tung engine hom nay"]):
            return "TODAY_PLANS"
        # When both history and top-engine wording are present, history is the requested row kind.
        if any(x in qf for x in history_phrases):
            return "HISTORY"
        if any(x in qf for x in top_engine_phrases):
            return "TOP_ENGINES"
        if any(x in qf for x in tomorrow_phrases):
            return "TOMORROW_PLAN"
        if qf.strip() in {"date", "kh date"} or any(x in qf for x in date_phrases) or ("forward" in qf and any(x in qf for x in ["ngày", "ngay", "xa nhất", "xa nhat", "latest"])):
            return "LATEST_DATE"

        # Range questions must be resolved before position/accountability parsing.
        # Traders phrase this in many orders: "biên dự kiến", "dự kiến dao động",
        # "expected high/low", "còn bao nhiêu điểm lên xuống".  A remembered
        # live price must enrich the answer, never hijack the intent into a trade plan.
        range_phrases = [
            "biên hôm nay", "biên hnay", "biên mới", "biên hiện tại", "biên thực tế", "biên dự kiến", "dự kiến biên",
            "dự báo biên", "forecast range", "biên dự báo", "range hôm nay",
            "dao động hôm nay", "dự kiến dao động", "dao động dự kiến",
            "dao động trong vùng", "vùng dao động", "khung dao động",
            "expected high", "expected low", "high low dự kiến", "high-low dự kiến",
            "đỉnh đáy dự kiến", "đáy đỉnh dự kiến", "biên còn lại",
            "còn lại lên xuống", "còn bao nhiêu điểm lên", "còn bao nhiêu điểm xuống",
        ]
        asks_range = any(k in q for k in range_phrases) or (
            any(k in q for k in ["biên", "range", "dao động", "high low", "high-low"])
            and any(k in q for k in ["dự kiến", "dự báo", "forecast", "hôm nay", "hnay", "bao nhiêu", "vùng nào"])
        )
        if asks_range:
            return "FORECAST_RANGE"
        level_exec_phrases = [
            "bình quân", "binh quan", "trung bình giá", "trung binh gia",
            "tăng khối lượng", "tang khoi luong", "tăng size", "tang size",
            "thêm vị thế", "them vi the", "add vị thế", "add vi the",
            "từng nấc", "tung nac", "nấc giá", "nac gia", "ladder",
            "vượt giả", "vuot gia", "false break", "false breakout",
            "gãy lại", "gay lai", "breakdown", "breakout",
            "gap up", "gap down", "nhảy gap", "nhay gap",
            "mức chung", "muc chung", "nấc chung", "nac chung",
            "trùng nhau giữa", "trung nhau giua", "đồng thuận mức", "dong thuan muc",
        ]
        if any(x in qf for x in level_exec_phrases) and not any(x in qf for x in ["cutloss", "cat loss", "cat lo", "atc", "go", "chan qua", "khong kip", "ko kip"]):
            return "LEVEL_EXECUTION"
        if SmartAdvisor._is_accountability_request(q) or SmartAdvisor._extract_position_claim(q)[0]:
            return "ACCOUNTABILITY_RECOVERY"
        if any(k in q for k in ["r5", "corrective overlay", "flip_hint", "flip hint", "r5 cancel", "r5 keep"]):
            return "R5_GUIDANCE"
        if any(k in q for k in ["chart forecast", "forecast chart", "biểu đồ forecast", "biểu đồ dự báo", "chart dự báo", "cái chart"]):
            return "FORECAST_CHART"
        bridge_long_phrases = [
            "long đến điểm short", "long den diem short", "long lên điểm short", "long len diem short",
            "long tới điểm short", "long toi diem short", "mua lên điểm chờ short", "mua len diem cho short",
            "long đến entry short", "long den entry short", "long lên entry short", "long len entry short",
            "sao không cho long", "sao ko cho long", "cho long đến", "cho long den",
            "đánh long hồi lên", "danh long hoi len", "bridge long", "countertrend bridge",
            "mình muốn kèo long của hệ", "minh muon keo long cua he", "tôi muốn kèo long của hệ", "toi muon keo long cua he",
            "long reclaim", "reclaim long", "được long reclaim", "duoc long reclaim",
        ]
        if any(x in qf for x in bridge_long_phrases):
            return "BRIDGE_LONG_TO_SHORT"
        bridge_short_phrases = [
            "short đến điểm long", "short den diem long", "short xuống điểm long", "short xuong diem long",
            "short tới điểm long", "short toi diem long", "bán xuống điểm chờ long", "ban xuong diem cho long",
            "short đến entry long", "short den entry long", "short xuống entry long", "short xuong entry long",
            "sao không cho short", "sao ko cho short", "cho short đến", "cho short den",
            "đánh short hồi xuống", "danh short hoi xuong", "bridge short", "short reclaim",
            "mình muốn kèo short của hệ", "minh muon keo short cua he", "tôi muốn kèo short của hệ", "toi muon keo short cua he",
        ]
        if any(x in qf for x in bridge_short_phrases):
            return "BRIDGE_SHORT_TO_LONG"
        if SmartAdvisor._is_countertrend_request(q):
            return "COUNTERTREND_PLAN"
        if SmartAdvisor._detect_requested_side(q):
            return "SIDE_PLAN"
        if numeric_values and max(numeric_values) >= 500 and any(k in q for k in ["open", "mở cửa", "ato"]):
            return "OPEN_SCENARIO"
        if any(k in q for k in ["bằng chứng", "evidence", "backtest", "bao nhiêu mẫu", "train", "holdout", "oos"]):
            return "EVIDENCE"
        if any(k in q for k in ["v44", "vi phạm gì", "vi pham gi", "điều kiện cảnh báo", "dieu kien canh bao", "cảnh báo gì", "canh bao gi"]):
            return "WARNINGS"
        scores: dict[str, int] = {
            "CURRENT_PLAN": 0,
            "FILL_STATUS": 0,
            "SCENARIO": 0,
            "OPEN_SCENARIO": 0,
            "TARGET": 0,
            "PRIORITY": 0,
            "WARNINGS": 0,
            "EVIDENCE": 0,
            "HISTORY": 0,
            "CONSENSUS": 0,
            "WHY": 0,
            "FRESHNESS": 0,
            "RISK": 0,
            "COMPARE": 0,
            "CHANGE": 0,
            "DATA_SOURCE": 0,
            "COUNTERTREND_PLAN": 0,
            "SIDE_PLAN": 0,
            "FORECAST_CHART": 0,
            "FORECAST_RANGE": 0,
            "QUALITY": 0,
            "R5_GUIDANCE": 0,
            "ACCOUNTABILITY_RECOVERY": 0,
            "ENGINE_PLAYBOOK": 0,
            "SYSTEM_PLAYBOOK": 0,
            "ENGINE_FILL_AUDIT": 0,
            "TOP_ENGINES": 0,
            "LATEST_DATE": 0,
        }
        if SmartAdvisor._is_countertrend_request(q):
            scores["COUNTERTREND_PLAN"] += 10
        keyword_map = {
            "WARNINGS": ["cảnh báo", "red flag", "nguy hiểm", "mâu thuẫn", "đắt giá", "warning", "v44", "vi phạm"],
            "EVIDENCE": ["backtest", "evidence", "bằng chứng", "bao nhiêu mẫu", "train", "holdout", "oos", "toàn kỳ"],
            "FRESHNESS": ["t+1", "t+2", "horizon", "basis", "stale", "tại sao lại có", "sao không có"],
            "FILL_STATUS": ["khớp", "fill", "vào được", "chạm entry", "đã vào", "chưa vào"],
            "SCENARIO": ["nếu", "giả sử", "thế giá", "khi giá", "gap", "hồi lên", "rơi xuống"],
            "TARGET": ["target", "mục tiêu", "chốt", "thoát", "mốc gần", "mốc xa"],
            "PRIORITY": ["ưu tiên", "hệ nào", "engine nào", "theo hệ nào", "kèo chính"],
            "HISTORY": ["lịch sử", "gần đây", "lời", "lỗ", "pnl", "kết quả", "phiên trước"],
            "CONSENSUS": ["đồng thuận", "cùng hướng", "xung đột", "khác hướng", "bao nhiêu hệ"],
            "WHY": ["tại sao", "vì sao", "giải thích", "sao lại", "lý do"],
            "QUALITY": ["chất lượng", "kèo ổn", "ổn không", "có ổn", "đáng đánh", "đáng vào", "có nên vào", "đánh giá kèo", "kèo sạch", "kèo tốt", "hôm nay sao", "nay sao", "nay thế nào", "đánh kiểu gì", "chơi kiểu gì", "kèo nào ăn được", "có cửa nào", "nên theo hướng nào"],
            "RISK": ["rủi ro", "stop", "cắt lỗ", "đuổi", "an toàn", "size", "units"],
            "COMPARE": ["so sánh", "khác nhau", "khác gì", "target nào", "engine"],
            "CHANGE": ["thay đổi gì", "có gì mới", "so với lúc nãy", "vừa rồi", "cập nhật"],
            "CURRENT_PLAN": ["kèo", "sáng nay", "hiện tại", "hôm nay", "long hay short", "làm gì"],
            "DATA_SOURCE": ["data nào", "dữ liệu nào", "nguồn nào", "excel", "kh entry", "nov24", "đang đọc gì"],
            "R5_GUIDANCE": ["r5", "corrective overlay", "flip_hint", "flip hint", "cancel", "keep"],
            "ENGINE_PLAYBOOK": ["simcarry", "simcarrry", "simcary", "simptkt", "12k", "ladder", "engine5"],
            "SYSTEM_PLAYBOOK": ["kèo của hệ", "cập nhật kèo", "update kèo", "khối lượng đi", "kịch bản nào thì đúng"],
            "ENGINE_FILL_AUDIT": ["engine nào khớp", "engines nào khớp", "hệ nào khớp", "lệnh nào khớp", "khớp được lệnh", "khớp dc lệnh"],
            "TOP_ENGINES": ["top engines", "list top engines", "engine hiện hành", "danh sách engine", "hệ nào đang chạy"],
            "LATEST_DATE": ["latest date", "ngày mới nhất", "kèo mới nhất", "kèo ngày nào"],
        }
        for intent, keys in keyword_map.items():
            for key in keys:
                if key in q:
                    scores[intent] += 2 if len(key) > 4 else 1
        if numeric_values and max(numeric_values) >= 500:
            scores["SCENARIO"] += 2
        # Operational intent takes precedence over generic explanation.
        priority = [
            "ACCOUNTABILITY_RECOVERY", "TOP_ENGINES", "LATEST_DATE", "ENGINE_FILL_AUDIT", "ENGINE_PLAYBOOK", "SYSTEM_PLAYBOOK", "R5_GUIDANCE", "FORECAST_CHART", "FORECAST_RANGE", "COUNTERTREND_PLAN", "OPEN_SCENARIO", "EVIDENCE", "WARNINGS", "QUALITY", "FRESHNESS", "FILL_STATUS", "SCENARIO", "TARGET",
            "PRIORITY", "RISK", "DATA_SOURCE", "HISTORY", "CONSENSUS", "CHANGE", "COMPARE", "WHY", "CURRENT_PLAN"
        ]
        return max(priority, key=lambda x: (scores[x], -priority.index(x))) if max(scores.values()) > 0 else "CURRENT_PLAN"

    @staticmethod
    def _fingerprint(context: dict[str, Any]) -> str:
        core = {
            "as_of": context.get("as_of"),
            "freshness": context.get("freshness"),
            "plans": [
                {
                    "key": _plan_key(p),
                    "entry": p.get("operational_entry"),
                    "target": p.get("operational_target"),
                    "action": p.get("risk_action"),
                }
                for p in context.get("active_plans", [])
            ],
            "warnings": [(w.get("id"), w.get("level"), w.get("plan_key")) for w in context.get("warnings", [])],
        }
        return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]

    def _build_context(self, as_of: str | None, snapshot: SessionSnapshot) -> dict[str, Any]:
        # Build each database independently, then merge by role.
        # Forward DB contributes only current/future plans; History DB contributes only settled history.
        forward_context = build_context(
            self.trades_path,
            ohlc_path=self.ohlc_path,
            as_of=as_of,
            snapshot=snapshot,
            warning_catalog_path=self.warning_catalog_path,
        )
        history_context = build_context(
            self.history_trades_path,
            ohlc_path=self.ohlc_path,
            as_of=as_of,
            snapshot=snapshot,
            warning_catalog_path=self.warning_catalog_path,
        )
        context = dict(forward_context)
        context["recent_history"] = list(history_context.get("recent_history", []))
        context["history_row_count"] = int(history_context.get("history_row_count", len(context["recent_history"])))
        context["forward_database"] = str(self.trades_path)
        context["history_database"] = str(self.history_trades_path)
        context["database_roles"] = {"forward": str(self.trades_path), "history_30n": str(self.history_trades_path)}
        return context

    @staticmethod
    def _find_plan(context: dict[str, Any], engine: str = "", horizon: str = "") -> dict[str, Any] | None:
        plans = context.get("active_plans", [])
        if engine:
            exact = [p for p in plans if p.get("engine") == engine and (not horizon or p.get("horizon") == horizon)]
            if exact:
                return exact[0]
        if horizon:
            exact = [p for p in plans if p.get("horizon") == horizon]
            if exact:
                return exact[0]
        operational = [p for p in plans if p.get("engine") == "gpt_simcarrry6"]
        return operational[0] if operational else (plans[0] if plans else None)

    @staticmethod
    def _resolve_engine(question: str, memory: ConversationMemory) -> str:
        q = SmartAdvisor._fold(question)
        if any(x in q for x in ["simcary", "simcarry", "simcarrry", "sim carry", "sim car", "gpt simcar"]):
            return "gpt_simcarrry6"
        if any(x in q for x in ["simptkt", "sim ptkt", "gpt simptkt"]):
            return "gpt_simptkt"
        if "12k" in q:
            return "engine5:12K_AllDay"
        if any(x in q for x in ["ladder", "cap0.3", "cap 0.3"]):
            return "engine5:AllDaysLadder_CAP0.3"
        if "engine5" in q or "r5" in q:
            return "engine5"
        if any(x in SmartAdvisor._norm(question) for x in ["hệ đó", "engine đó", "nó", "kèo đó"]):
            return memory.focus_engine
        return ""

    @staticmethod
    def _resolve_horizon(question: str, memory: ConversationMemory) -> str:
        q = question.lower().replace(" ", "")
        if "t+2" in q or "t2" in q:
            return "t+2"
        if "t+1" in q or "t1" in q:
            return "t+1"
        if re.search(r"\bt\b", question.lower()):
            return "t"
        if any(x in question.lower() for x in ["kèo đó", "hệ đó", "nó"]):
            return memory.focus_horizon
        return ""

    @staticmethod
    def _plan_label(plan: dict[str, Any]) -> str:
        return f"{_engine_display(plan.get('engine',''), plan.get('profile',''))} {plan.get('horizon') or ''}".strip()

    @staticmethod
    def _state_map(context: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
        return {tuple(x["plan_key"]): x["state"] for x in context.get("plan_states", [])}

    @staticmethod
    def _warning_for_plan(context: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
        key = tuple(_plan_key(plan))
        return [w for w in context.get("warnings", []) if tuple(w.get("plan_key", ())) == key]

    @staticmethod
    def _severe_warnings(context: dict[str, Any]) -> list[dict[str, Any]]:
        return [w for w in context.get("warnings", []) if str(w.get("level", "")).upper() in {"BLOCKER", "CRITICAL", "HIGH"}]

    @staticmethod
    def _metric_vi(value: Any, decimals: int = 1, signed: bool = False) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "NA"
        prefix = "+" if signed and number > 0 else ""
        text = f"{abs(number):,.{decimals}f}"
        text = text.replace(",", "X").replace(".", ",").replace("X", ".")
        if number < 0:
            prefix = "-"
        return prefix + text

    @staticmethod
    def _warning_rank(level: str) -> int:
        return {"BLOCKER": 0, "CRITICAL": 1, "HIGH": 2, "MEDIUM": 3, "NOTICE": 4, "INFO": 5}.get(str(level).upper(), 9)

    def _dominant_warning(self, context: dict[str, Any], plan: dict[str, Any] | None = None) -> dict[str, Any] | None:
        warnings = list(context.get("warnings", []))
        if plan:
            plan_key = tuple(_plan_key(plan))
            matched = [w for w in warnings if tuple(w.get("plan_key", ())) == plan_key]
            if matched:
                warnings = matched
        if not warnings:
            return None
        warnings.sort(key=lambda w: (self._warning_rank(str(w.get("level", ""))), 0 if w.get("id") == "BAND_DIRECTION_CONTRADICTION" else 1))
        return warnings[0]

    def _warning_plan(self, context: dict[str, Any], warning: dict[str, Any], preferred: dict[str, Any] | None = None) -> dict[str, Any] | None:
        key = tuple(warning.get("plan_key", ()))
        if preferred and (not key or tuple(_plan_key(preferred)) == key):
            return preferred
        for plan in context.get("active_plans", []):
            if key and tuple(_plan_key(plan)) == key:
                return plan
        return preferred or self._primary_plan(context)

    def _dominant_warning_lines(
        self,
        context: dict[str, Any],
        plan: dict[str, Any] | None = None,
        snapshot: SessionSnapshot | None = None,
        *,
        evidence: bool = True,
        action: bool = True,
    ) -> list[str]:
        warning = self._dominant_warning(context, plan)
        if not warning:
            return []
        matched_plan = self._warning_plan(context, warning, plan)
        wid = warning.get("id")
        if wid != "BAND_DIRECTION_CONTRADICTION":
            lines = [f"Cảnh báo {warning.get('level')}: {warning.get('title')}. {warning.get('interpretation') or warning.get('detail') or ''}".strip()]
            decision_policy = warning.get("decision_policy") or {}
            if action and decision_policy.get("customer_action"):
                lines.append(str(decision_policy.get("customer_action")))
            elif action and warning.get("required_response"):
                lines.append(str(warning.get("required_response")))
            return lines

        direction = str(warning.get("direction") or (matched_plan or {}).get("direction") or "NA").upper()
        raw_entry = (matched_plan or {}).get("original_entry")
        raw_target = (matched_plan or {}).get("original_target")
        op_entry = (matched_plan or {}).get("operational_entry")
        op_target = (matched_plan or {}).get("operational_target")

        if direction == "LONG":
            definition = (
                f"Nói dễ hiểu: bản gốc bảo LONG nhưng mức được gọi là đỉnh kỳ vọng {_fmt(raw_target)} "
                f"lại thấp hơn giá tham chiếu {_fmt(raw_entry)}. Hai con số đang nằm ngược phía với hướng LONG, "
                "nên không được dùng cặp giá gốc để vào lệnh."
            )
        elif direction == "SHORT":
            definition = (
                f"Nói dễ hiểu: bản gốc bảo SHORT nhưng mức được gọi là đáy kỳ vọng {_fmt(raw_target)} "
                f"lại cao hơn giá tham chiếu {_fmt(raw_entry)}. Hai con số đang nằm ngược phía với hướng SHORT, "
                "nên không được dùng cặp giá gốc để vào lệnh."
            )
        else:
            definition = "Nói dễ hiểu: hướng dự báo và hai mức giá gốc nằm sai phía nhau, nên cặp giá gốc không được dùng để đặt lệnh."
        lines = [definition + " (Trong dữ liệu nội bộ, tình huống này được gắn nhãn V44.)"]

        metrics = warning.get("evidence_metrics") or {}
        full = metrics.get("full") or {}
        splits = metrics.get("splits") or []
        profile = self._warning_evidence_profile(warning)
        if evidence and full:
            raw_split = [float(x.get("raw_pnl_points")) for x in splits if x.get("raw_pnl_points") is not None]
            swap_split = [float(x.get("swap_operational_pnl_points")) for x in splits if x.get("swap_operational_pnl_points") is not None]
            lines.append(
                "Kiểm định 2018–2026: cách dùng cặp giá gốc có "
                f"{self._metric_vi(full.get('raw_touched_trades'), 0)} giao dịch, WR {self._metric_vi(full.get('raw_wr_pct'))}%, "
                f"PnL {self._metric_vi(full.get('raw_pnl_points'), signed=True)} điểm"
                + (" và thua ở cả giai đoạn xây dựng lẫn hai giai đoạn kiểm tra độc lập." if raw_split and all(x < 0 for x in raw_split) else ".")
            )
            if profile.get("swap_positive_all"):
                lines.append(
                    f"Sau khi hệ đảo lại đúng vai trò vào–ra, tổng PnL là {self._metric_vi(full.get('swap_operational_pnl_points'), signed=True)} điểm"
                    + (" và dương ở cả ba giai đoạn kiểm định." if swap_split and all(x > 0 for x in swap_split) else ".")
                )
        elif profile.get("raw_negative_all"):
            if profile.get("swap_positive_all") and matched_plan and bool(matched_plan.get("entry_target_swap_applied")):
                lines.append("Dữ liệu dài hạn cho thấy cặp giá gốc thua ổn định, còn cặp đã sửa có hiệu quả dương ở các giai đoạn kiểm định; vì vậy bỏ cặp gốc nhưng không mặc định bỏ cả phiên.")
            else:
                lines.append("Dữ liệu dài hạn cho thấy cặp giá gốc thua ổn định; không được đặt lệnh theo hai mức gốc.")

        if action:
            if matched_plan and bool(matched_plan.get("entry_target_swap_applied")):
                if direction == "SHORT":
                    lines.append(
                        f"Kế hoạch được phép dùng là SHORT đã sửa: chờ giá chạm {_fmt(op_entry)} rồi quay xuống dưới vùng này mới xét vào; mốc chốt {_fmt(op_target)} chỉ có hiệu lực sau khi đã khớp vùng vào đúng thứ tự."
                    )
                elif direction == "LONG":
                    lines.append(
                        f"Kế hoạch được phép dùng là LONG đã sửa: chờ giá chạm {_fmt(op_entry)} rồi lấy lại/giữ trên vùng này mới xét vào; mốc chốt {_fmt(op_target)} chỉ có hiệu lực sau khi đã khớp vùng vào đúng thứ tự."
                    )
                else:
                    lines.append(f"Chỉ dùng cặp đã sửa {_fmt(op_entry)} → {_fmt(op_target)} và phải xác nhận entry trước target.")
            else:
                lines.append("Chưa có cặp giá sửa hợp lệ nên chưa phát lệnh; chờ outputs mới hoặc một setup độc lập đã được hệ xác nhận.")

        if snapshot and matched_plan:
            price = snapshot.live_price
            high = snapshot.session_high
            low = snapshot.session_low
            if direction == "SHORT" and op_entry is not None and price is not None:
                if high is not None and float(high) >= float(op_entry) and float(price) > float(op_entry):
                    lines.append(
                        f"OHLC hiện tại chưa đạt điều kiện SHORT: High {_fmt(high)} đã xuyên entry {_fmt(op_entry)} nhưng giá hiện tại {_fmt(price)} vẫn ở trên entry. Cần mất lại vùng vào và retest không lấy lại mới xác nhận cú vượt thất bại."
                    )
                elif high is not None and float(high) >= float(op_entry) and float(price) <= float(op_entry):
                    lines.append(
                        f"OHLC đã tạo điều kiện từ chối vùng SHORT: High {_fmt(high)} chạm/xuyên {_fmt(op_entry)} và giá hiện tại {_fmt(price)} đã quay xuống dưới; vẫn phải tuân quyền R5 trước khi vào."
                    )
                elif high is not None and float(high) < float(op_entry):
                    lines.append(f"OHLC chưa chạm entry SHORT {_fmt(op_entry)} vì High mới là {_fmt(high)}; không SHORT đuổi ở dưới vùng chờ.")
            elif direction == "LONG" and op_entry is not None and price is not None:
                if low is not None and float(low) <= float(op_entry) and float(price) < float(op_entry):
                    lines.append(
                        f"OHLC hiện tại chưa đạt điều kiện LONG: Low {_fmt(low)} đã xuyên entry {_fmt(op_entry)} nhưng giá hiện tại {_fmt(price)} vẫn ở dưới entry. Cần lấy lại vùng vào và retest giữ được mới xác nhận false breakdown."
                    )
                elif low is not None and float(low) <= float(op_entry) and float(price) >= float(op_entry):
                    lines.append(
                        f"OHLC đã tạo điều kiện reclaim cho LONG: Low {_fmt(low)} chạm/xuyên {_fmt(op_entry)} và giá hiện tại {_fmt(price)} đã lấy lại vùng; vẫn phải tuân quyền R5 trước khi vào."
                    )
                elif low is not None and float(low) > float(op_entry):
                    lines.append(f"OHLC chưa chạm entry LONG {_fmt(op_entry)} vì Low mới là {_fmt(low)}; không LONG đuổi ở trên vùng chờ.")
        return lines

    def _max_size_from_plan(plan: dict[str, Any] | None) -> float | None:
        if not plan:
            return None
        units = plan.get("units")
        try:
            if units is not None:
                return float(units)
        except (TypeError, ValueError):
            pass
        m = re.search(r"MAX[_=]?([0-9]+(?:\.[0-9]+)?)", str(plan.get("volume_rule") or ""), flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        return None

    @staticmethod
    def _range_position(snapshot: SessionSnapshot) -> tuple[float | None, float | None]:
        if snapshot.live_price is None or snapshot.session_high is None or snapshot.session_low is None:
            return None, None
        span = float(snapshot.session_high) - float(snapshot.session_low)
        if span <= 0:
            return None, span
        return (float(snapshot.live_price) - float(snapshot.session_low)) / span, span

    def _warning_evidence_profile(self, warning: dict[str, Any] | None) -> dict[str, Any]:
        metrics = (warning or {}).get("evidence_metrics") or {}
        full = metrics.get("full") or {}
        splits = metrics.get("splits") or []
        raw_split = [float(x.get("raw_pnl_points")) for x in splits if x.get("raw_pnl_points") is not None]
        swap_split = [float(x.get("swap_operational_pnl_points")) for x in splits if x.get("swap_operational_pnl_points") is not None]
        return {
            "raw_negative_all": bool(raw_split) and all(x < 0 for x in raw_split),
            "swap_positive_all": bool(swap_split) and all(x > 0 for x in swap_split),
            "raw_full": full.get("raw_pnl_points"),
            "swap_full": full.get("swap_operational_pnl_points"),
            "raw_wr": full.get("raw_wr_pct"),
            "raw_trades": full.get("raw_touched_trades"),
            "splits": splits,
        }

    def _warning_action_summary(self, warning: dict[str, Any] | None, plan: dict[str, Any] | None) -> str:
        if not warning or warning.get("id") != "BAND_DIRECTION_CONTRADICTION":
            return ""
        profile = self._warning_evidence_profile(warning)
        if profile["raw_negative_all"] and profile["swap_positive_all"] and plan and plan.get("entry_target_swap_applied"):
            side = str(plan.get("direction") or "").upper()
            entry = plan.get("operational_entry")
            target = plan.get("operational_target")
            if side == "SHORT":
                rule = f"chỉ SHORT khi giá chạm {_fmt(entry)} rồi quay xuống dưới; mốc chốt {_fmt(target)} chỉ tính sau khi vùng vào đã được khớp"
            elif side == "LONG":
                rule = f"chỉ LONG khi giá chạm {_fmt(entry)} rồi reclaim/giữ trên; mốc chốt {_fmt(target)} chỉ tính sau khi vùng vào đã được khớp"
            else:
                rule = f"chỉ dùng cặp đã sửa {_fmt(entry)} → {_fmt(target)} và đúng thứ tự khớp lệnh"
            return f"Nói gọn: bỏ hai mức gốc bị đảo; {rule}. Không cần đứng ngoài cả ngày, nhưng OHLC và quyền R5 phải xác nhận trước khi vào."
        if profile["raw_negative_all"]:
            return "Nói gọn: không dùng hai mức giá gốc vì chúng nằm ngược với hướng dự báo; chỉ giao dịch khi outputs có cặp sửa hợp lệ và OHLC xác nhận."
        return ""

    def _levels_between(self, context: dict[str, Any], start: float, end: float) -> list[float]:
        lo, hi = sorted([float(start), float(end)])
        levels: set[float] = set()
        for plan in context.get("active_plans", []):
            for key in ("operational_entry", "operational_target", "forecast"):
                value = plan.get(key)
                try:
                    if value is not None and lo <= float(value) <= hi:
                        levels.add(float(value))
                except (TypeError, ValueError):
                    pass
        levels.update([float(start), float(end)])
        return sorted(levels, reverse=start > end)

    @staticmethod
    def _r5_control(context: dict[str, Any]) -> dict[str, Any]:
        return context.get("r5_control") or {
            "stage": "UNKNOWN", "current_action": "NO_SIGNAL", "explicit_action": False,
            "contract": "NO_R5_AUTHORITY", "max_position": None, "profiles": [], "rules": [],
        }

    @staticmethod
    def _r5_size_text(context: dict[str, Any]) -> str:
        cap = (context.get("r5_control") or {}).get("max_position")
        if cap is None:
            return "vị thế thăm dò nhỏ"
        return f"khởi đầu 0,10 và tối đa {_fmt(float(cap), 2)} vị thế"

    def _r5_countertrend_gate(
        self,
        context: dict[str, Any],
        system_side: str,
        requested_side: str,
        snapshot: SessionSnapshot,
    ) -> tuple[bool, list[str], str]:
        r5 = self._r5_control(context)
        action = str(r5.get("current_action") or "NO_SIGNAL").upper()
        opposite = requested_side in {"LONG", "SHORT"} and requested_side != system_side
        if not opposite:
            return True, [], "ALIGNED"
        if action == "CANCEL":
            return False, [
                "R5 đang CANCEL/units 0: NO TRADE với kèo frozen; CANCEL không tự động biến thành lệnh đảo chiều.",
                "Chỉ được dựng kèo ngược mới khi outputs phát FLIP_HINT rõ ràng hoặc có một setup độc lập đã backtest; hiện chưa có quyền đó.",
            ], "BLOCKED_CANCEL"
        if action == "KEEP":
            return False, [
                f"R5 đang KEEP hướng frozen {system_side}: không được gọi {requested_side} là kèo R5 hay tự động flip.",
                "Nếu chỉ muốn ăn nhịp bridge thì phải coi đó là scalp ngoài kèo chính, size nhỏ, đủ OHLC xác nhận và đóng trước entry hệ; R5 vẫn ưu tiên hướng frozen.",
            ], "BLOCKED_KEEP"
        if action == "FLIP_HINT":
            if None in {snapshot.session_open, snapshot.session_high, snapshot.session_low, snapshot.live_price}:
                return False, [
                    f"R5 có FLIP_HINT sang {requested_side}, nhưng đây mới là gợi ý đảo có điều kiện; cần đủ Open–High–Low–giá live mới được kích hoạt.",
                    "Không vào chỉ vì thấy chữ FLIP_HINT và không dùng full size.",
                ], "WAIT_LIVE_CONFIRM"
            return True, [
                f"R5 đã phát FLIP_HINT: được phép đánh giá {requested_side} ngược hướng frozen, nhưng chỉ sau xác nhận live và với {self._r5_size_text(context)}.",
            ], "AUTHORIZED_FLIP_HINT"
        if action == "PRE_OPEN":
            prefix = [
                "R5 hiện mới ở PRE_OPEN/PENDING_OHLCV: chưa có KEEP, CANCEL hay FLIP_HINT chính thức trong outputs.",
            ]
            if None in {snapshot.session_open, snapshot.session_high, snapshot.session_low, snapshot.live_price}:
                prefix.append("Vì chưa đủ OHLC live, mọi kèo ngược chỉ là kịch bản chuẩn bị, chưa phải lệnh được R5 xác nhận. Gửi Open–High–Low–giá hiện tại để dựng nhánh bridge hoặc nhánh theo frozen.")
                return False, prefix, "PREOPEN_WAIT_OHLC"
            prefix.append(
                f"OHLC khách cung cấp chỉ đủ dựng kịch bản R5 tạm thời; có thể thăm dò theo điều kiện với {self._r5_size_text(context)}, nhưng phải chờ base cập nhật action OPEN để xác nhận KEEP/CANCEL/FLIP_HINT."
            )
            return True, prefix, "PREOPEN_SCENARIO_ONLY"
        return False, [
            "Outputs chưa có action R5 hợp lệ; không được gắn nhãn kèo ngược là do R5 cho phép.",
        ], "NO_R5_AUTHORITY"

    def _answer_r5_guidance(
        self,
        context: dict[str, Any],
        focus_plan: dict[str, Any] | None,
        question: str,
        snapshot: SessionSnapshot,
    ) -> list[str]:
        p = focus_plan or self._primary_plan(context)
        if not p:
            return ["Không có kèo frozen active để R5 hiệu chỉnh; chưa thể tư vấn theo R5."]
        system_side = str(p.get("direction") or "").upper()
        requested = self._detect_requested_side(question)
        r5 = self._r5_control(context)
        action = str(r5.get("current_action") or "NO_SIGNAL").upper()
        cap = r5.get("max_position")

        # Khi khách hỏi một hướng ngược cụ thể, hợp đồng R5 phải đi thẳng vào quyền/không quyền giao dịch.
        if requested and requested != system_side:
            allowed, gate, mode = self._r5_countertrend_gate(context, system_side, requested, snapshot)
            lines = list(gate)
            if allowed:
                action_lines = self._intraday_action_lines(context, p, snapshot, requested_side=requested, _skip_r5_gate=True)
                lines.extend(action_lines)
            elif mode == "PREOPEN_WAIT_OHLC":
                lines.append("Gửi Open–High–Low–giá hiện tại; hệ sẽ dựng hai nhánh: bridge ngược tới entry hoặc theo hướng frozen sau rejection/reclaim.")
            return self._limit_answer_lines(lines, max_lines=7)

        lines = [f"R5 hiện ở trạng thái {action}; đây là corrective overlay của kèo frozen {system_side}, không phải một engine phát kèo độc lập."]
        if r5.get("conflict"):
            actions = ", ".join(r5.get("source_actions") or [])
            lines.append(f"Các profile R5 đang lệch action ({actions}); hệ lấy quyền ưu tiên an toàn CANCEL > FLIP_HINT > KEEP, hiện chốt {action}.")
        if action == "PRE_OPEN":
            lines.append("Outputs đang PRE_OHLCV nên R5 chưa chốt KEEP/CANCEL/FLIP_HINT; chỉ được dựng kịch bản, chưa được nói R5 đã cho đảo hướng.")
        elif action == "KEEP":
            lines.append(f"KEEP nghĩa là giữ kèo frozen {system_side}; không auto-flip. Vị thế tối đa theo profile R5 hiện có là {_fmt(cap,2) if cap is not None else 'không ghi'}.")
        elif action == "CANCEL":
            lines.append("CANCEL/units 0 nghĩa là NO TRADE, PnL 0 cho kèo bị hủy; không lấy kết quả giả định sau cancel để vào lệnh hoặc tự đảo chiều.")
        elif action == "FLIP_HINT":
            lines.append("FLIP_HINT chỉ mở một kịch bản đảo có điều kiện; phải có Open/intraday xác nhận và dùng size giảm, không tự động đảo ngay tại Open.")
        else:
            lines.append("Không có action R5 rõ trong outputs; chỉ được mô tả kịch bản, không được gắn quyền cho R5.")
        action_lines = self._intraday_action_lines(context, p, snapshot, requested_side=system_side, _skip_r5_gate=True)
        if action_lines:
            lines.extend(action_lines[:3])
        return self._limit_answer_lines(lines, max_lines=7)

    def _intraday_action_lines(
        self,
        context: dict[str, Any],
        plan: dict[str, Any],
        snapshot: SessionSnapshot,
        requested_side: str | None = None,
        _skip_r5_gate: bool = False,
    ) -> list[str]:
        side = str(plan.get("direction") or "").upper()
        entry = plan.get("operational_entry")
        target = plan.get("operational_target")
        price = snapshot.live_price
        if side not in {"LONG", "SHORT"} or entry is None or price is None:
            return []
        entry = float(entry)
        target_f = float(target) if target is not None else None
        price = float(price)
        pos, span = self._range_position(snapshot)
        open_price = float(snapshot.session_open) if snapshot.session_open is not None else None
        high = float(snapshot.session_high) if snapshot.session_high is not None else None
        low = float(snapshot.session_low) if snapshot.session_low is not None else None
        full_ohlc = (
            open_price is not None and high is not None and low is not None and span not in (None, 0)
            and (high > max(open_price, price) + 1e-9 or low < min(open_price, price) - 1e-9)
        )
        tolerance = max(1.5, (float(span) * 0.12) if span else 2.0)
        requested = (requested_side or "").upper() or None
        lines: list[str] = []
        r5 = self._r5_control(context)
        r5_action = str(r5.get("current_action") or "NO_SIGNAL").upper()
        counter_side = "LONG" if side == "SHORT" else "SHORT"

        if r5_action == "CANCEL":
            return [
                "R5 đang CANCEL/units 0: NO TRADE với kèo frozen; không mở lệnh thuận và cũng không tự đảo chiều.",
                "Chờ outputs phát action mới. Chỉ FLIP_HINT rõ ràng mới mở lại việc đánh giá phía đối diện.",
            ]
        if r5_action == "FLIP_HINT" and requested == side:
            detail = (
                f"OHLC live đã có: tiếp tục quản lý ứng viên {counter_side} theo điều kiện đã nêu; chỉ xét lại {side} khi R5 đổi về KEEP hoặc outputs phát kế hoạch mới."
                if full_ohlc
                else f"Muốn xét {counter_side} phải có đủ Open–High–Low–giá live; FLIP_HINT vẫn không phải lệnh đảo tự động."
            )
            return [
                f"R5 đang FLIP_HINT ngược hướng frozen {side}: không mở mới {side} theo kèo cũ.",
                detail,
            ]

        allow_aligned_branch = not (r5_action == "FLIP_HINT" and requested is None)
        if requested in {None, side} and allow_aligned_branch:
            if side == "SHORT":
                touched = high is not None and high >= entry
                rejected = touched and price <= entry
                if rejected:
                    lines.append(f"OHLC đã thỏa điều kiện kích hoạt SHORT: High {_fmt(high)} chạm/xuyên entry {_fmt(entry)}, sau đó giá hiện tại {_fmt(price)} quay xuống dưới entry — tức cú vượt đã thất bại.")
                    lines.append(f"Hành động: chỉ triển khai SHORT theo ladder/khối lượng của engine khi R5 cho phép; mốc chốt {_fmt(target_f)}. Nếu giá lấy lại và giữ trên {_fmt(entry)} thì đóng/không vào thêm, tuyệt đối không bình quân.")
                    return lines
                if touched and price > entry:
                    lines.append(f"Chưa SHORT. OHLC đang vi phạm đúng điều kiện kích hoạt: High {_fmt(high)} đã vượt entry {_fmt(entry)} nhưng giá hiện tại {_fmt(price)} vẫn nằm trên entry, nên chưa có tín hiệu từ chối/failed breakout.")
                    lines.append(f"Hành động: không SHORT và không bình quân. Chỉ xét lại khi giá mất {_fmt(entry)} rồi retest không lấy lại; nếu còn giữ trên {_fmt(entry)} hoặc phá tiếp High {_fmt(high)} thì kèo SHORT bị vô hiệu. Target {_fmt(target_f)} chỉ có ý nghĩa sau một fill hợp lệ.")
                    return lines
                if abs(price - entry) <= tolerance:
                    if price <= entry:
                        lines.append(f"Kết luận: giá đang sát vùng SHORT {_fmt(entry)}; chỉ vào khi thấy từ chối vùng này và quay xuống dưới entry, chưa có rejection thì chờ.")
                    else:
                        lines.append(f"Chưa SHORT. Giá hiện tại {_fmt(price)} vẫn trên entry {_fmt(entry)}, nên điều kiện bắt buộc 'chạm entry rồi quay xuống dưới' chưa xảy ra; chờ mất entry và retest không lấy lại.")
                    if high is not None:
                        lines.append(f"Mốc quyết định là entry {_fmt(entry)}, không phải một nhãn cảnh báo: giữ trên entry/High {_fmt(high)} thì bỏ SHORT; quay xuống dưới entry và retest thất bại mới mở lại kịch bản. Target {_fmt(target_f)} chỉ dùng sau fill.")
                    return lines
                if price < entry:
                    gap_to_entry = max(0.0, entry - (high if high is not None else price))
                    if snapshot.is_completed_bar and high is not None and high < entry:
                        lines.append(f"Kết luận phiên: entry SHORT {_fmt(entry)} chưa hề được chạm — High chỉ {_fmt(high)}, còn thiếu {_fmt(gap_to_entry)} điểm. Vì không có fill nên kèo này không phát sinh giao dịch.")
                        if target_f is not None and low is not None and low <= target_f:
                            lines.append(f"Low {_fmt(low)} có đi qua target {_fmt(target_f)}, nhưng xảy ra trong một phiên không chạm entry; không được tính là kèo thắng và cũng không được bán đuổi sau đó.")
                    elif target_f is not None and price <= target_f:
                        lines.append(f"Kết luận: Giá {_fmt(price)} — không SHORT đuổi; đứng ngoài với kèo SHORT vì giá đã đi qua mục tiêu {_fmt(target_f)} trước khi chạm vùng vào {_fmt(entry)}, nên kèo thuận hệ đang WAIT_ENTRY chứ không phải đã thắng; chỉ chờ hồi lên đúng entry, không bán đuổi.")
                    else:
                        lines.append(f"Chưa SHORT vì phiên chưa chạm entry {_fmt(entry)}: High mới {_fmt(high)} và giá hiện tại {_fmt(price)}. Còn thiếu khoảng {_fmt(gap_to_entry)} điểm tính từ High; chờ giá lên đúng vùng, không bán đuổi ở dưới.")
                else:
                    lines.append(f"Chưa SHORT. Giá đã vượt vùng vào {_fmt(entry)} nhưng chưa có bằng chứng quay xuống; không bình quân bán lên, chỉ xem lại khi mất entry và retest thất bại.")
            else:
                touched = low is not None and low <= entry
                reclaimed = touched and price > entry
                if reclaimed:
                    lines.append(f"OHLC đã thỏa điều kiện kích hoạt LONG: Low {_fmt(low)} chạm/xuyên entry {_fmt(entry)}, sau đó giá hiện tại {_fmt(price)} lấy lại trên entry — tức cú thủng đã thất bại.")
                    lines.append(f"Hành động: chỉ triển khai LONG theo ladder/khối lượng của engine khi R5 cho phép; mốc chốt {_fmt(target_f)}. Nếu giá mất lại và giữ dưới {_fmt(entry)} thì đóng/không vào thêm, tuyệt đối không bình quân.")
                    return lines
                if touched and price < entry:
                    lines.append(f"Chưa LONG. OHLC đang vi phạm đúng điều kiện kích hoạt: Low {_fmt(low)} đã thủng entry {_fmt(entry)} nhưng giá hiện tại {_fmt(price)} vẫn nằm dưới entry, nên chưa có reclaim/false breakdown.")
                    lines.append(f"Hành động: không LONG và không bình quân. Chỉ xét lại khi giá lấy lại {_fmt(entry)} rồi retest giữ được; nếu còn nằm dưới {_fmt(entry)} hoặc phá tiếp Low {_fmt(low)} thì kèo LONG bị vô hiệu. Target {_fmt(target_f)} chỉ có ý nghĩa sau một fill hợp lệ.")
                    return lines
                if abs(price - entry) <= tolerance:
                    if price >= entry:
                        lines.append(f"Kết luận: giá đang sát vùng LONG {_fmt(entry)}; chỉ vào khi giữ/reclaim được entry, chưa có xác nhận thì chờ.")
                    else:
                        lines.append(f"Chưa LONG. OHLC đang vi phạm điều kiện vào LONG: giá hiện tại {_fmt(price)} vẫn ở dưới vùng vào {_fmt(entry)}; chờ lấy lại vùng vào và retest giữ được để xác nhận false breakdown.")
                    if low is not None:
                        lines.append(f"Low {_fmt(low)} cho biết vùng entry đã bị xuyên. Còn giữ dưới Low/vùng vào thì bỏ ý tưởng LONG; mục tiêu {_fmt(target_f)} chỉ được dùng sau khi vùng vào khớp đúng thứ tự.")
                    return lines
                if price > entry:
                    gap_to_entry = max(0.0, (low if low is not None else price) - entry)
                    if snapshot.is_completed_bar and low is not None and low > entry:
                        lines.append(f"Kết luận phiên: entry LONG {_fmt(entry)} chưa hề được chạm — Low chỉ {_fmt(low)}, còn cách {_fmt(gap_to_entry)} điểm. Vì không có fill nên kèo này không phát sinh giao dịch.")
                        if target_f is not None and high is not None and high >= target_f:
                            lines.append(f"High {_fmt(high)} có đi qua target {_fmt(target_f)}, nhưng xảy ra trong một phiên không chạm entry; không được tính là kèo thắng và cũng không được mua đuổi sau đó.")
                    elif target_f is not None and price >= target_f:
                        lines.append(f"Kết luận: Giá {_fmt(price)} — không LONG đuổi; đứng ngoài với kèo LONG vì giá đã đi qua mục tiêu {_fmt(target_f)} trước khi chạm vùng vào {_fmt(entry)}, nên kèo thuận hệ đang WAIT_ENTRY chứ không phải đã thắng; chỉ chờ điều chỉnh về đúng entry, không mua đuổi.")
                    else:
                        lines.append(f"Chưa LONG. Low/giá hiện tại chưa chạm vùng vào {_fmt(entry)}; còn cách khoảng {_fmt(price-entry)} điểm, nên chờ điều chỉnh đúng vùng thay vì mua đuổi ở trên.")
                else:
                    lines.append(f"Chưa LONG. Giá đã thủng vùng vào {_fmt(entry)} nhưng chưa reclaim; không bình quân mua xuống, chỉ xem lại khi lấy lại vùng vào và retest giữ được.")

        counter_gate_lines: list[str] = []
        counter_allowed = True
        if not _skip_r5_gate and (requested == counter_side or requested is None):
            allowed, gate_lines, gate_mode = self._r5_countertrend_gate(context, side, counter_side, snapshot)
            counter_allowed = allowed
            counter_gate_lines = list(gate_lines)
            if not allowed:
                if requested == counter_side:
                    return gate_lines
                # Câu hỏi chung: giữ kết luận thuận hệ nếu có, nhưng tuyệt đối không rơi sang auto-countertrend.
                if lines:
                    lines.extend(gate_lines[:2] if r5_action == "PRE_OPEN" else gate_lines[:1])
                    return lines
                return gate_lines
            elif requested == counter_side and gate_lines:
                lines.extend(gate_lines)
        prepassed = target_f is not None and ((side == "SHORT" and price <= target_f) or (side == "LONG" and price >= target_f))
        implicit_flip = requested is None and r5_action == "FLIP_HINT"
        if counter_allowed and requested == counter_side:
            if requested is None and counter_gate_lines:
                # Với câu hỏi chung, chỉ chèn dòng hành động R5 cô đọng để không lấn át quyết định giá.
                lines.extend(counter_gate_lines[-1:])
            if not full_ohlc:
                if open_price is not None:
                    rel = price - open_price
                    lines.append(f"Giá hiện {_fmt(price)}, mở cửa {_fmt(open_price)} ({'cao hơn' if rel > 0 else 'thấp hơn' if rel < 0 else 'bằng'} {_fmt(abs(rel))} điểm), nhưng chưa có High–Low đầy đủ nên chưa kích hoạt {counter_side} ngược nhịp.")
                else:
                    lines.append(f"Có thể chuẩn bị {counter_side} ngược nhịp, nhưng cần đủ Open–High–Low và giá hiện tại để xác nhận vị trí trong biên; chưa đủ dữ kiện thì chưa vào.")
                return lines
            assert pos is not None and span is not None and open_price is not None and high is not None and low is not None
            if counter_side == "LONG":
                if price >= entry - tolerance:
                    lines.append(f"LONG ngược nhịp đã tới vùng thoát {_fmt(entry)}: nếu đang có vị thế thì chốt hết; nếu chưa có thì không mở mới. Chỉ cân nhắc đảo SHORT sau khi giá từ chối vùng này và mất lại vùng vào.")
                    return lines
                if pos >= 0.80:
                    checkpoint = _fmt(target_f) if target_f is not None and price >= target_f else _fmt(high)
                    lines.append(f"Không mở LONG ngược nhịp mới: giá {_fmt(price)} đã ở {pos:.0%} biên phiên, gần High {_fmt(high)}. Nếu đã có LONG scalp thì chốt bớt quanh mốc {checkpoint}; chỉ giữ phần nhỏ khi vượt và giữ được High, mất Open thì giảm.")
                    return lines
                if price <= open_price:
                    lines.append(f"Chưa LONG: giá {_fmt(price)} chưa giữ được Open {_fmt(open_price)}; chỉ kích hoạt khi reclaim Open và không tạo Low mới dưới {_fmt(low)}.")
                    return lines
                checkpoints = [x for x in self._levels_between(context, price, entry) if x > price]
                ladder = " → ".join(_fmt(x) for x in checkpoints[:4]) if checkpoints else _fmt(entry)
                lines.append(f"Kết luận: có thể LONG scalp ngược nhịp theo khung R5, {self._r5_size_text(context)} vì giá {_fmt(price)} đang trên Open {_fmt(open_price)} và ở {pos:.0%} biên phiên; không coi là kèo chính.")
                lines.append(f"Mốc xử lý lấy đúng từ outputs: {ladder}; chốt dần, đóng hết trước/ở vùng SHORT {_fmt(entry)}.")
                lines.append(f"Nếu mất lại Open {_fmt(open_price)} thì giảm; thủng Low {_fmt(low)} thì thoát, không bình quân. Vượt High {_fmt(high)} mới xác nhận nhịp hồi khỏe hơn.")
                return lines
            if price <= entry + tolerance:
                lines.append(f"SHORT ngược nhịp đã tới vùng thoát {_fmt(entry)}: nếu đang có vị thế thì chốt hết; nếu chưa có thì không mở mới. Chỉ cân nhắc đảo LONG sau khi giá giữ/reclaim vùng này.")
                return lines
            if pos <= 0.20:
                checkpoint = _fmt(target_f) if target_f is not None and price <= target_f else _fmt(low)
                lines.append(f"Không mở SHORT ngược nhịp mới: giá {_fmt(price)} đã ở {pos:.0%} biên phiên, gần Low {_fmt(low)}. Nếu đã có SHORT scalp thì chốt bớt quanh mốc {checkpoint}; chỉ giữ phần nhỏ khi thủng và giữ dưới Low, vượt Open thì giảm.")
                return lines
            if price >= open_price:
                lines.append(f"Chưa SHORT: giá {_fmt(price)} chưa mất Open {_fmt(open_price)}; chỉ kích hoạt khi mất lại Open và không tạo High mới trên {_fmt(high)}.")
                return lines
            checkpoints = [x for x in self._levels_between(context, price, entry) if x < price]
            ladder = " → ".join(_fmt(x) for x in checkpoints[:4]) if checkpoints else _fmt(entry)
            lines.append(f"Kết luận: có thể SHORT scalp ngược nhịp theo khung R5, {self._r5_size_text(context)} vì giá {_fmt(price)} đang dưới Open {_fmt(open_price)} và ở {pos:.0%} biên phiên; không coi là kèo chính.")
            lines.append(f"Mốc xử lý lấy đúng từ outputs: {ladder}; chốt dần, đóng hết trước/ở vùng LONG {_fmt(entry)}.")
            lines.append(f"Nếu vượt lại Open {_fmt(open_price)} thì giảm; vượt High {_fmt(high)} thì thoát, không bình quân. Thủng Low {_fmt(low)} mới xác nhận nhịp giảm khỏe hơn.")
            return lines
        return lines

    @staticmethod
    def _warning_text(w: dict[str, Any]) -> str:
        mitigation = " (đã được hệ giảm rủi ro)" if w.get("mitigated") else ""
        if w.get("id") == "BAND_DIRECTION_CONTRADICTION":
            direction = str(w.get("direction") or "").upper()
            base = (
                f"[{w.get('level')}] Hai mức giá gốc nằm ngược với hướng {direction or 'dự báo'}{mitigation}: "
                "không dùng cặp gốc đặt lệnh; chỉ dùng cặp đã sửa và phải xác nhận bằng OHLC/R5."
            )
            full = (w.get("evidence_metrics") or {}).get("full", {})
            if full:
                base += (
                    f" Toàn kỳ: {int(full.get('raw_touched_trades', 0))} giao dịch theo cặp gốc, "
                    f"WR {full.get('raw_wr_pct', 0):.1f}%, PnL {full.get('raw_pnl_points', 0):+.1f} điểm."
                )
            return base
        return f"[{w.get('level')}] {w.get('title')}{mitigation}: {w.get('interpretation') or w.get('detail') or ''}"

    @staticmethod
    def _format_evidence_metrics(w: dict[str, Any]) -> list[str]:
        metrics = w.get("evidence_metrics") or {}
        if not metrics:
            return []
        period_names = {
            "TRAIN 2018-2022": "giai đoạn xây dựng 2018–2022",
            "OOS1 2023-2024": "kiểm tra độc lập 2023–2024",
            "OOS2 2025-2026": "kiểm tra độc lập 2025–2026",
        }
        lines = ["  Cách đo: chờ cặp giá gốc được chạm rồi tính kết quả theo hướng dự báo, trước khi áp dụng cơ chế sửa entry–target."]
        for row in metrics.get("splits", []):
            name = period_names.get(str(row.get("period")), str(row.get("period")))
            lines.append(
                f"  - {name}: {int(row.get('raw_touched_trades', 0))} giao dịch | "
                f"WR {row.get('raw_wr_pct', 0):.1f}% | PnL {row.get('raw_pnl_points', 0):+.1f} | "
                f"trung bình {row.get('raw_avg_points_per_trade', 0):+.2f}/giao dịch | "
                f"sụt giảm lớn nhất {row.get('raw_maxdd_points', 0):+.1f}."
            )
        full = metrics.get("full", {})
        if full:
            lines.append(
                f"  - Toàn kỳ: {int(full.get('raw_touched_trades', 0))} giao dịch | "
                f"WR {full.get('raw_wr_pct', 0):.1f}% | PnL {full.get('raw_pnl_points', 0):+.1f} | "
                f"trung bình {full.get('raw_avg_points_per_trade', 0):+.2f}/giao dịch | "
                f"sụt giảm lớn nhất {full.get('raw_maxdd_points', 0):+.1f}."
            )
            lines.append(
                f"  Sau khi sửa đúng vai trò vùng vào–mốc chốt, cùng nhóm tạo PnL {full.get('swap_operational_pnl_points', 0):+.1f} điểm; "
                "vì vậy cảnh báo loại cặp giá gốc, không mặc định hủy mọi kế hoạch đã sửa."
            )
        return lines

    @staticmethod
    def _format_plan(plan: dict[str, Any], state: dict[str, Any] | None = None) -> str:
        line = (
            f"{SmartAdvisor._plan_label(plan)}: {plan.get('direction')} | "
            f"entry {_fmt(plan.get('operational_entry'))} | target {_fmt(plan.get('operational_target'))} | "
            f"{plan.get('risk_action') or plan.get('action_or_outcome') or 'không ghi action'}"
        )
        if state:
            line += f" | {state.get('message','')}"
        return line

    def _direct_current_plan(self, context: dict[str, Any], focus_plan: dict[str, Any] | None) -> list[str]:
        plans = context.get("active_plans", [])
        con = context.get("consensus", {})
        lines: list[str] = []
        if not plans:
            return ["Không có kế hoạch forward hợp lệ cho ngày yêu cầu."]
        if con.get("is_unanimous"):
            lines.append(f"Kèo hiện tại đồng thuận {con.get('direction')} trên {con.get('count')}/{con.get('count')} kế hoạch.")
        else:
            lines.append(f"Chưa đồng thuận tuyệt đối; hướng đa số là {con.get('direction')} ({con.get('strength', 0):.0%}).")
        states = self._state_map(context)
        ordered = [focus_plan] + [p for p in plans if p is not focus_plan] if focus_plan else plans
        for p in ordered:
            if p:
                lines.append("- " + self._format_plan(p, states.get(_plan_key(p))))
        latest = context.get("latest_completed_ohlc") or {}
        entries = [p.get("operational_entry") for p in plans if p.get("operational_entry") is not None]
        if entries and latest.get("close") is not None:
            if con.get("direction") == "SHORT" and latest["close"] < min(entries):
                lines.append("Đây là kèo chờ hồi lên entry để SHORT, không phải SHORT đuổi ở vùng thấp.")
            if con.get("direction") == "LONG" and latest["close"] > max(entries):
                lines.append("Đây là kèo chờ điều chỉnh về entry để LONG, không phải LONG đuổi ở vùng cao.")
        return lines

    def _answer_evidence(self, context: dict[str, Any], focus_plan: dict[str, Any] | None, question: str) -> list[str]:
        warnings = self._warning_for_plan(context, focus_plan) if focus_plan else context.get("warnings", [])
        if not warnings:
            return ["Kế hoạch đang hỏi không kích hoạt cảnh báo backtest trong catalog hiện tại."]
        q = question.lower()
        selected = warnings
        if "mâu thuẫn" in q:
            selected = [w for w in warnings if w.get("id") == "BAND_DIRECTION_CONTRADICTION"]
        elif "stale" in q or "cũ" in q:
            selected = [w for w in warnings if w.get("id") == "STALE_EXPECTED_BAND"]
        elif "t+2" in q:
            selected = [w for w in warnings if w.get("id") == "T2_BAND_CONFIDENCE_REDUCED"]
        elif "fallback" in q:
            selected = [w for w in warnings if w.get("id") == "PTKT_ATR_FALLBACK_ACTIVE"]
        if not selected:
            return ["Không có evidence đúng loại đang hỏi trên kế hoạch active này."]
        lines = []
        for w in selected:
            lines.append(f"- {w.get('title')}: {w.get('evidence_scope','NA')}")
            lines.extend(self._format_evidence_metrics(w))
            if w.get("required_response"):
                lines.append(f"  Cách hệ xử lý: {w.get('required_response')}")
        return lines

    @staticmethod
    def _plan_label(plan: dict[str, Any]) -> str:
        label = _engine_display(plan.get("engine", ""), plan.get("profile", ""))
        horizon = str(plan.get("horizon") or "").strip()
        return f"{label} {horizon}".strip()

    def _rows_for_forward_date(self, context: dict[str, Any], which: str = "today") -> list[dict[str, Any]]:
        plans = list(context.get("all_forward_plans", []))
        dates = sorted({p.get("date_ts") for p in plans if p.get("date_ts") is not None})
        if not dates:
            return []
        selected = dates[0] if which == "today" else dates[-1]
        return [p for p in plans if p.get("date_ts") == selected]

    def _answer_today_plans(self, context: dict[str, Any]) -> list[str]:
        rows = self._rows_for_forward_date(context, "today")
        if not rows:
            return ["Database hiện chưa có row FORWARD cho phiên hiện hành."]
        lines = [f"Kèo phiên hiện hành trong database: {rows[0].get('date')} — {len(rows)} row, tách riêng từng engine/profile:"]
        for p in rows:
            lines.append(
                f"- {self._plan_label(p)}: {p.get('direction')} {_fmt(p.get('operational_entry'))} → {_fmt(p.get('operational_target'))}; "
                f"size {p.get('volume_rule') or 'theo output'}; trạng thái {p.get('risk_action') or p.get('action_or_outcome') or 'NA'}."
            )
        return lines

    def _answer_forward_count(self, context: dict[str, Any]) -> list[str]:
        plans = list(context.get("all_forward_plans", []))
        by_date: dict[str, int] = {}
        for p in plans:
            by_date[str(p.get("date") or "NA")] = by_date.get(str(p.get("date") or "NA"), 0) + 1
        detail = ", ".join(f"{d}: {n}" for d,n in sorted(by_date.items())) or "không có"
        return [f"Database có {len(plans)} row FORWARD hợp lệ.", f"Theo ngày: {detail}.", f"Nguồn: {context.get('source_file','NA')}."]

    def _answer_data_source(self, context: dict[str, Any]) -> list[str]:
        plans = list(context.get("all_forward_plans", []))
        latest = max((p.get("date_ts") for p in plans if p.get("date_ts") is not None), default=None)
        latest_text = next((p.get("date") for p in plans if p.get("date_ts") == latest), "NA") if latest is not None else "NA"
        history_rows = list(context.get("recent_history", []))
        return [
            f"Database kèo hiện tại/tương lai: {context.get('forward_database') or context.get('source_file','NA')} — {len(plans)} row FORWARD; ngày mới nhất {latest_text}.",
            f"Database lịch sử/hiệu suất 30N: {context.get('history_database','NA')} — {len(history_rows)} row HISTORY đang nạp.",
            "Hai nguồn bị khóa theo vai trò; không lấy database forward để tính hiệu suất và không lấy database history để phát kèo tương lai.",
        ]

    def _answer_forecast_ranges(self, context: dict[str, Any], question: str) -> list[str]:
        qf = self._fold(question)
        plans = list(context.get("all_forward_plans", []))
        def matches(p):
            label = self._fold(self._plan_label(p))
            if "simcarrry6" in qf or "simcarry6" in qf:
                if "simcarrry6" not in label and "simcarry6" not in label: return False
            if "engine5" in qf and str(p.get("engine") or "").lower() != "engine5": return False
            if "alldays" in qf and "alldays" not in label: return False
            if "12k" in qf and "12k" not in label: return False
            both_horizons = "t+1" in qf and "t+2" in qf
            if not both_horizons and "t+1" in qf and str(p.get("horizon") or "").lower() != "t+1": return False
            if not both_horizons and "t+2" in qf and str(p.get("horizon") or "").lower() != "t+2": return False
            if "hôm nay" in qf or "hom nay" in qf:
                dates=sorted({x.get("date_ts") for x in plans if x.get("date_ts") is not None})
                if dates and p.get("date_ts") != dates[0]: return False
            return True
        rows=[p for p in plans if matches(p)]
        if not rows:
            return ["Không có row forward đúng engine/horizon được hỏi; không lấy kèo khác trả thay."]
        lines=[]
        for p in rows:
            a=p.get("operational_entry"); b=p.get("operational_target")
            if a is None or b is None: continue
            lo,hi=sorted([float(a),float(b)])
            lines.append(f"- {self._plan_label(p)} ngày {p.get('date')}: vùng forecast/kèo {_fmt(lo)} – {_fmt(hi)}, rộng {_fmt(hi-lo)} điểm; {p.get('direction')} {_fmt(a)} → {_fmt(b)}.")
        if not lines:
            return ["Các row phù hợp không có đủ hai mốc để mô tả vùng; không tự bịa biên."]
        return ["Biên/vùng theo từng row cùng engine và horizon:"]+lines

    def _answer_latest_date(self, context: dict[str, Any]) -> list[str]:
        # "Latest" means the maximum FORWARD date present in the database, not
        # the earliest active trading date selected for today's execution.
        plans = list(context.get("all_forward_plans", []))
        if not plans:
            return ["Database hiện chưa có kèo forward hợp lệ để xác định ngày mới nhất."]
        latest = max((p.get("date_ts") for p in plans if p.get("date_ts") is not None), default=None)
        rows = [p for p in plans if latest is not None and p.get("date_ts") == latest]
        date_text = rows[0].get("date") if rows else context.get("as_of")
        lines = [f"Ngày kèo forward mới nhất trong database: {date_text}."]
        for p in rows:
            lines.append(
                f"{self._plan_label(p)}: {p.get('direction')} {_fmt(p.get('operational_entry'))} → "
                f"{_fmt(p.get('operational_target'))}; size {p.get('volume_rule') or 'theo output'}; "
                f"trạng thái {p.get('risk_action') or p.get('action_or_outcome') or 'NA'}."
            )
        lines.append(f"Nguồn đang đọc: {context.get('source_file', 'NA')}.")
        return lines

    def _answer_tomorrow_plan(self, context: dict[str, Any]) -> list[str]:
        plans = list(context.get("all_forward_plans", []))
        dates = sorted({p.get("date_ts") for p in plans if p.get("date_ts") is not None})
        if not dates:
            return ["Database hiện chưa có kèo forward để xác định kèo ngày mai."]
        # In a forward snapshot, the earliest date is the current execution day;
        # the next distinct date is tomorrow. If only one date exists, report that
        # no separate tomorrow row has been supplied instead of recycling today.
        if len(dates) < 2:
            today_rows = [p for p in plans if p.get("date_ts") == dates[0]]
            day = today_rows[0].get("date") if today_rows else "NA"
            return [f"Database chỉ có kèo cho {day}; chưa có row forward riêng cho ngày kế tiếp."]
        tomorrow = dates[1]
        rows = [p for p in plans if p.get("date_ts") == tomorrow]
        lines = [f"Kèo ngày kế tiếp trong database: {rows[0].get('date') if rows else 'NA'}."]
        for p in rows:
            lines.append(
                f"{self._plan_label(p)}: {p.get('direction')} {_fmt(p.get('operational_entry'))} → "
                f"{_fmt(p.get('operational_target'))}; size {p.get('volume_rule') or 'theo output'}; "
                f"trạng thái {p.get('risk_action') or p.get('action_or_outcome') or 'NA'}."
            )
        return lines

    def _answer_top_engines(self, context: dict[str, Any]) -> list[str]:
        """Return the database snapshot exactly as supplied: up to three rows per engine label.

        The LAST3+FORWARD TSV is already curated as three visible rows for each top
        engine.  Do not collapse it to only the newest forward row.  History and
        forward rows remain visibly separated and retain their own dates.
        """
        forwards = list(context.get("all_forward_plans", []))
        history = list(context.get("recent_history", []))
        rows = history + forwards
        if not rows:
            return ["Database hiện chưa có engine hiện hành hoặc kèo hợp lệ."]

        # Group by the exact displayed engine/profile label. Horizon is part of the
        # label for SIMCARRRY6, while engine5 profiles must stay separate.
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for r in rows:
            key = (str(r.get("engine") or ""), str(r.get("profile") or ""))
            groups.setdefault(key, []).append(r)

        # Rank engines by their newest available row, then by match score. This
        # follows the current database rather than a hard-coded engine order.
        ranked = sorted(
            groups.items(),
            key=lambda kv: (
                max((x.get("date_ts") for x in kv[1] if x.get("date_ts") is not None), default=pd.Timestamp.min),
                max((x.get("match_score") or 0 for x in kv[1]), default=0),
            ),
            reverse=True,
        )
        lines = ["Top engines hiện hành — hiển thị đúng tối đa 3 trades của từng engine theo database:"]
        for idx, (_, engine_rows) in enumerate(ranked[:3], 1):
            # Exact trade dedupe only. Never collapse distinct dates or a forward
            # row merely because entry/target happens to match another engine.
            dedup: dict[tuple[Any, ...], dict[str, Any]] = {}
            for r in engine_rows:
                k = (
                    r.get("date"), r.get("kind"), r.get("engine"), r.get("profile"), r.get("horizon"),
                    r.get("direction"), r.get("operational_entry"), r.get("operational_target"),
                )
                dedup[k] = r
            selected = sorted(
                dedup.values(),
                key=lambda r: (r.get("date_ts") or pd.Timestamp.min, 1 if str(r.get("kind", "")).startswith("FORWARD") else 0),
                reverse=True,
            )[:3]
            label = _engine_display(selected[0].get("engine", ""), selected[0].get("profile", "")) if selected else "Engine không rõ"
            lines.append(f"{idx}. {label} ({len(selected)} trades):")
            for r in selected:
                row_kind = "FUTURE" if str(r.get("kind", "")).startswith("FORWARD") else "HISTORY"
                if row_kind == "HISTORY":
                    status = r.get("action_or_outcome") or r.get("win_loss") or r.get("r5_action") or "NA"
                else:
                    status = r.get("risk_action") or r.get("action_or_outcome") or "NA"
                pnl = r.get("pnl_points")
                tail = f"; PnL {_fmt(pnl)} điểm" if row_kind == "HISTORY" and pnl is not None else ""
                lines.append(
                    f"   - {row_kind} {r.get('date')} {str(r.get('horizon') or '').strip()}: {r.get('direction')} "
                    f"{_fmt(r.get('operational_entry'))} → {_fmt(r.get('operational_target'))}; {status}{tail}."
                )
        return lines


    @staticmethod
    def _history_is_executed(row: dict[str, Any]) -> bool:
        status = " ".join(str(row.get(k) or "") for k in ("action_or_outcome", "win_loss", "r5_action")).upper()
        pnl = row.get("pnl_points")
        if pnl is None:
            return False
        if any(x in status for x in ("CANCEL", "NOFILL", "NO_FILL", "WAIT_ENTRY", "PENDING")):
            return False
        return abs(float(pnl)) > 1e-12

    def _requested_performance_window(self, question: str, default: int = 3) -> tuple[int, str]:
        q = self._fold(question)
        q = q.replace("hai muoi phien", "20 phien").replace("ba muoi phien", "30 phien").replace("ba muoi trades", "30 trades").replace("muoi phien", "10 phien").replace("tuan gan day", "7 phien")
        q = re.sub(r"(\d{1,3})\s*giao dich", r"\1 trades", q)
        q = re.sub(r"(\d{1,3})\s*sessions", r"\1 phien", q)
        # 30D/20D/10D are common trader shorthand for a day window.
        md = re.search(r"(?<!\d)(\d{1,3})\s*d(?:\b|$)", q)
        if md:
            return max(1, min(int(md.group(1)), 100)), "dates"
        m = re.search(r"(?<!\d)(\d{1,3})\s*(ngay|phien|trade|trades|lenh)", q)
        if not m:
            return default, "dates"
        n = max(1, min(int(m.group(1)), 100))
        unit = m.group(2)
        return n, ("trades" if unit in {"trade", "trades", "lenh"} else "dates")

    def _performance_rows(self, context: dict[str, Any], window: int = 3, unit: str = "dates") -> tuple[list[dict[str, Any]], list[str]]:
        rows = [r for r in context.get("recent_history", []) if r.get("date_ts") is not None and r.get("pnl_points") is not None]
        rows.sort(key=lambda r: r.get("date_ts") or pd.Timestamp.min, reverse=True)
        if unit == "trades":
            picked = rows[:window]
            dates=[]
            for r in picked:
                d=str(r.get("date") or "")
                if d and d not in dates: dates.append(d)
            return picked, dates
        dates: list[str] = []
        for r in rows:
            d = str(r.get("date") or "")
            if d and d not in dates:
                dates.append(d)
            if len(dates) >= window:
                break
        return [r for r in rows if r.get("date") in dates], dates

    def _answer_recent_performance(self, context: dict[str, Any], question: str = "", include_reverse: bool = False) -> list[str]:
        window, unit = self._requested_performance_window(question, 3)
        rows, dates = self._performance_rows(context, window, unit)
        if not rows:
            return ["Database đang nạp không có row HISTORY đã settled kèm PnL; chưa thể báo hiệu suất thực tế."]
        total_available = len([r for r in context.get("recent_history", []) if r.get("date_ts") is not None and r.get("pnl_points") is not None])
        distinct_available = len({str(r.get("date") or "") for r in context.get("recent_history", []) if r.get("date_ts") is not None and r.get("pnl_points") is not None})
        requested_label = f"{window} lệnh settled gần nhất" if unit == "trades" else f"{window} phiên settled gần nhất"
        lines = [f"Yêu cầu: {requested_label}. Database đang nạp có {total_available} row HISTORY/PnL trên {distinct_available} ngày; nguồn {Path(context.get('history_database') or context.get('source_file','')).name}."]
        actual = len(rows) if unit == "trades" else len(dates)
        if actual < window:
            lines.append(f"Chưa đủ cửa sổ yêu cầu: chỉ có {actual}/{window} {'lệnh' if unit=='trades' else 'ngày'} trong file đang đọc; không tự bù bằng row FORWARD hay metadata 'last30'.")
        # Per-engine transparent summary.
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(self._plan_label(r), []).append(r)
        for label, grp in groups.items():
            executed=[r for r in grp if self._history_is_executed(r)]
            pnl=sum(float(r.get('pnl_points') or 0.0) for r in executed)
            wins=sum(1 for r in executed if float(r.get('pnl_points') or 0.0)>0)
            losses=sum(1 for r in executed if float(r.get('pnl_points') or 0.0)<0)
            wr=(100.0*wins/len(executed)) if executed else 0.0
            cancelled=len(grp)-len(executed)
            lines.append(f"- {label}: {len(grp)} row, {len(executed)} lệnh thực thi, W/L {wins}/{losses}, WR {wr:.2f}%, PnL {pnl:+.2f} điểm, cancel/no-fill {cancelled}.")
            if include_reverse:
                lines.append(f"  Đánh ngược cơ học các lệnh đã thực thi: khoảng {-pnl:+.2f} điểm trước chi phí bổ sung; không phải backtest chiến lược ngược.")
        if dates:
            lines.append("Ngày bao phủ: " + ", ".join(dates) + ".")
        lines.append("Không cộng hai profile trùng tín hiệu thành PnL danh mục; công bố riêng từng engine/profile để tránh đếm đôi.")
        return self._limit_answer_lines(lines, max_lines=18)

    def _answer_engine_performance(self, context: dict[str, Any], question: str = "") -> list[str]:
        # A bare request such as "hiệu suất" means the available recent-history base,
        # not an arbitrary 3-session window and never the remembered OHLC/current plan.
        qf = self._fold(question).strip()
        bare = {
            "hieu suat", "performance", "pnl", "wr", "win rate",
            "chart hieu suat", "bieu do hieu suat", "do thi hieu suat",
            "chart performance", "performance chart", "chart pnl", "bieu do pnl",
        }
        if qf in bare:
            return self._answer_recent_performance(context, question="30 trades gần nhất", include_reverse=False)
        return self._answer_recent_performance(context, question=question, include_reverse=False)

    def _answer_advice_validation(self, context: dict[str, Any], question: str = "") -> list[str]:
        rows, dates = self._performance_rows(context, 30, "trades")
        long_audit = self._load_bridge_long_audit()
        short_audit = self._load_bridge_short_audit()
        window, unit = self._requested_performance_window(question, 30)
        lines = ["Mức kiểm định phải tách theo từng loại tư vấn:"]
        requested = f"{window} {'lệnh' if unit=='trades' else 'ngày'}" if question else "cửa sổ được hỏi"
        lines.append(f"1. Base outputs hiện hành: yêu cầu {requested}; đối chiếu được {len(rows)} row HISTORY/PnL trên {len(dates)} ngày trong {Path(context.get('history_database') or context.get('source_file','')).name}. Chỉ được tuyên bố đúng phạm vi file này; nhãn 'last30' trong note không thay thế 30 row thật.")
        for idx, (name, audit) in enumerate((("LONG reclaim", long_audit), ("SHORT reclaim", short_audit)), 2):
            full = audit.get("full", {})
            status = str(audit.get("status") or "NOT_BACKTESTED")
            provenance = audit.get("source_note") or audit.get("provenance") or "Không có mô tả nguồn."
            if bool(audit.get("promoted")) and status.upper() in {"PASS", "PROMOTED"}:
                lines.append(f"{idx}. {name}: PASS advisory; {int(full.get('trades',0))} lệnh, WR {float(full.get('wr_pct',0)):.2f}%, PnL {float(full.get('pnl_points',0)):+.2f}, MaxDD {float(full.get('max_dd_points',0)):.2f} điểm. Nguồn: {provenance}")
            else:
                lines.append(f"{idx}. {name}: {status}; chưa được phép trình bày như kịch bản đã kiểm định.")
        lines.append("4. Biên/gap/fill/ladder là diễn giải outputs live-safe, không tự động có backtest PnL riêng.")
        lines.append("5. PnL đánh ngược cơ học chỉ là đối dấu lệnh đã chạy, không phải chiến lược ngược đã kiểm định.")
        return self._limit_answer_lines(lines, max_lines=10)

    def _answer_performance_full_audit(self, context: dict[str, Any], question: str = "") -> list[str]:
        lines = self._answer_recent_performance(context, question=question, include_reverse=True)
        lines.append("--- Mức kiểm định ---")
        lines.extend(self._answer_advice_validation(context, question)[1:])
        return self._limit_answer_lines(lines, max_lines=24)

    def _answer_history_top(self, context: dict[str, Any], question: str) -> list[str]:
        rows = list(context.get("recent_history", []))
        if not rows:
            return ["Không tìm thấy lịch sử phù hợp trong cửa sổ dữ liệu đang nạp."]
        # Default contract: one most-recent settled trade for each of up to three top engine labels.
        seen: set[tuple[str, str, str]] = set()
        picked: list[dict[str, Any]] = []
        for r in rows:
            key = (str(r.get("engine") or ""), str(r.get("profile") or ""))
            if key in seen:
                continue
            seen.add(key); picked.append(r)
            if len(picked) >= 3:
                break
        # If fewer than three distinct labels exist, fill with the next latest rows without duplicating the exact trade key.
        exact = {(r.get("date"), r.get("engine"), r.get("profile"), r.get("horizon")) for r in picked}
        for r in rows:
            k = (r.get("date"), r.get("engine"), r.get("profile"), r.get("horizon"))
            if len(picked) >= 3:
                break
            if k not in exact:
                exact.add(k); picked.append(r)
        lines = ["Ba kèo lịch sử gần nhất, ưu tiên một kèo cho mỗi engine/profile top:"]
        for i, r in enumerate(picked[:3], 1):
            pnl = r.get("pnl_points")
            lines.append(
                f"{i}. {r.get('date')} — {self._plan_label(r)}: {r.get('direction')} "
                f"{_fmt(r.get('operational_entry'))} → {_fmt(r.get('operational_target'))}; "
                f"{r.get('action_or_outcome') or r.get('win_loss') or 'NA'}, PnL {_fmt(pnl)} điểm, R5 {r.get('r5_action') or 'NA'}."
            )
        return lines

    def _answer_history(self, context: dict[str, Any], question: str) -> list[str]:
        requested_date = self._extract_date(question)
        rows = context.get("recent_history", [])
        if requested_date:
            rows = [r for r in rows if r.get("date") == requested_date]
        if not rows:
            return ["Không tìm thấy lịch sử phù hợp trong cửa sổ dữ liệu đang nạp."]
        lines = ["Kết quả lịch sử gần nhất; không cộng chồng nhiều engine/horizon thành một danh mục:"]
        for r in rows[:8]:
            lines.append(
                f"- {r.get('date')} | {_engine_display(r.get('engine',''), r.get('profile',''))} {r.get('horizon') or ''} | "
                f"{r.get('direction')} | {r.get('action_or_outcome')} | PnL {_fmt(r.get('pnl_points'))} điểm."
            )
        return lines

    def _answer_freshness(self, context: dict[str, Any], question: str) -> list[str]:
        fresh = context.get("freshness", {})
        lines = [
            f"OHLC hoàn thành mới nhất: {fresh.get('latest_completed_ohlc_date') or 'NA'}.",
            f"Forward mới hợp lệ: {fresh.get('fresh_forward_count', 0)}; forward basis cũ bị loại: {fresh.get('stale_forward_count', 0)}.",
        ]
        for p in context.get("all_forward_plans", context.get("active_plans", [])):
            lines.append(
                f"- {self._plan_label(p)} ngày {p.get('date')}: basis {p.get('basis_date') or 'NA'}, trạng thái {p.get('freshness_status')}."
            )
        stale = context.get("stale_forward_plans", [])
        if stale:
            lines.append("Các plan stale không được nâng thành kèo chính:")
            for p in stale[:4]:
                lines.append(f"- {self._plan_label(p)} ngày {p.get('date')}: {p.get('freshness_reason')}")
        return lines

    def _answer_scenario(
        self,
        context: dict[str, Any],
        focus_plan: dict[str, Any] | None,
        question: str,
        base_snapshot: SessionSnapshot,
    ) -> list[str]:
        numbers = self._parse_numbers(question)
        if not numbers:
            return ["Hãy nêu một mức giá hoặc snapshot Open/High/Low để mô phỏng trạng thái fill."]
        price = numbers[-1]
        snap = SessionSnapshot(
            as_of=base_snapshot.as_of,
            live_price=price,
            session_open=base_snapshot.session_open,
            session_high=max(x for x in [base_snapshot.session_high, price] if x is not None),
            session_low=min(x for x in [base_snapshot.session_low, price] if x is not None),
        )
        qnorm = self._norm(question)
        explicit_focus = any(k in qnorm for k in ["simcarry", "simcarrry", "simptkt", "12k", "ladder", "t+1", "t+2", "kèo đó", "keo do"])
        plans = [focus_plan] if (focus_plan and explicit_focus) else context.get("active_plans", [])
        lines = [f"Giá hiện tại {_fmt(price)}:"]
        for p in plans:
            if not p:
                continue
            state = evaluate_live_plan(p, snap)
            entry = p.get("operational_entry")
            distance = abs(float(entry) - float(price)) if entry is not None else None
            if state.get("state") in {"WAIT_ENTRY", "WAIT_ENTRY_TARGET_PREPASSED"}:
                lines.append(
                    f"- {self._plan_label(p)} chưa khớp; vùng vào {_fmt(entry)}, còn {_fmt(distance)} điểm. "
                    f"Không {p.get('direction') or 'vào lệnh'} đuổi."
                )
                if state.get("state") == "WAIT_ENTRY_TARGET_PREPASSED":
                    lines.append("  Target đã đi qua trước fill; phải chạm lại sau fill mới được tính thắng.")
            else:
                lines.append(f"- {self._plan_label(p)}: {state.get('message')}")
        lines.append("Đây là trạng thái theo giá vừa nhập, không phải dự báo giá sẽ tiếp tục đi theo hướng nào.")
        return lines

    def _answer_open_scenario(
        self,
        context: dict[str, Any],
        snapshot: SessionSnapshot,
    ) -> list[str]:
        open_price = snapshot.session_open
        if open_price is None:
            return ["Chưa có giá Open hợp lệ để đánh giá kịch bản."]
        plans = context.get("active_plans", [])
        states = self._state_map(context)
        con = context.get("consensus", {})
        lines = [f"ĐÁNH GIÁ OPEN {_fmt(open_price)}:"]
        if not plans:
            lines.append("Không có plan forward hợp lệ; không suy đoán lệnh mới.")
            return lines
        if con.get("is_unanimous"):
            lines.append(f"- Direction đồng thuận {con.get('direction')} trên {con.get('count')}/{con.get('count')} kế hoạch, nhưng đồng thuận hướng không đồng nghĩa được vào market tại Open.")
        else:
            lines.append(f"- Direction chưa đồng thuận tuyệt đối; hướng đa số {con.get('direction') or 'NA'} ({con.get('strength', 0):.0%}).")

        no_chase = True
        prepassed = False
        for p in plans:
            state = states.get(_plan_key(p), {})
            entry = p.get("operational_entry")
            target = p.get("operational_target")
            direction = p.get("direction")
            distance = abs(entry - open_price) if entry is not None else None
            lines.append(
                f"- {self._plan_label(p)}: {direction} | Open cách entry {_fmt(distance)} điểm | "
                f"vùng vào {_fmt(entry)} | target {_fmt(target)} | {state.get('message','')}"
            )
            plan_warnings = self._warning_for_plan(context, p)
            contradiction = next((w for w in plan_warnings if w.get("id") == "BAND_DIRECTION_CONTRADICTION"), None)
            if contradiction:
                original_entry = p.get("original_entry")
                original_target = p.get("original_target")
                structural_match = (
                    direction == "LONG" and original_target is not None and original_entry is not None and original_target < original_entry
                ) or (
                    direction == "SHORT" and original_target is not None and original_entry is not None and original_target > original_entry
                )
                open_match = (
                    direction == "LONG" and original_target is not None and original_target < open_price
                ) or (
                    direction == "SHORT" and original_target is not None and original_target > open_price
                )
                if structural_match:
                    lines.append(
                        f"  CẢNH BÁO BACKTEST: đúng nhóm direction-band mâu thuẫn vì raw {direction} "
                        f"OriginalEntry {_fmt(original_entry)}, Expected target {_fmt(original_target)}."
                    )
                if open_match:
                    lines.append(
                        f"  Open {_fmt(open_price)} cũng nằm sai phía so với raw expected target; đây là biểu hiện live của cùng red flag, "
                        "nhưng điều kiện backtest chuẩn được xác định bằng OriginalEntry, không phải lấy Open để thay định nghĩa."
                    )
                lines.extend(self._format_evidence_metrics(contradiction))
            if state.get("state") == "WAIT_ENTRY_TARGET_PREPASSED":
                prepassed = True
            if state.get("state") not in {"WAIT_ENTRY", "WAIT_ENTRY_TARGET_PREPASSED"}:
                no_chase = False

        if no_chase:
            side = con.get("direction") or "kèo"
            lines.append(f"KHUYẾN NGHỊ HỆ: tại Open là NO TRADE / WAIT_ENTRY; không {side} đuổi. Chỉ hành động khi giá quay về đúng operational entry và điều kiện fill được xác nhận.")
        if prepassed:
            lines.append(
                "CẢNH BÁO LIVE-INTEGRITY: ít nhất một target đã bị đi qua trước khi có fill. "
                "Không được báo target hit. Nếu giá hồi lên entry rồi khớp, target phải được chạm lại sau fill và cần bar/event log để xác nhận thứ tự."
            )
        simcar = next((p for p in plans if p.get("engine") == "gpt_simcarrry6"), None)
        simptkt = next((p for p in plans if p.get("engine") == "gpt_simptkt"), None)
        if simcar:
            lines.append("- Vai trò: SIMCARRRY6 là kế hoạch operational chính; đọc operational entry/target sau swap, không dùng raw levels để vào lệnh.")
        if simptkt:
            lines.append("- SIMPTKT chỉ là xác nhận PTKT độc lập; không biến direction SHORT thành lệnh SHORT market ở Open.")
        lines.append("Schema không có stop-loss chuẩn, nên AI không tự bịa điểm cắt lỗ.")
        return lines

    def _answer_target(self, context: dict[str, Any], focus_plan: dict[str, Any] | None, memory: ConversationMemory, question: str) -> list[str]:
        plans = context.get("active_plans", [])
        q = question.lower()
        if focus_plan:
            return [
                f"{self._plan_label(focus_plan)} có target operational {_fmt(focus_plan.get('operational_target'))}. "
                f"Raw target chỉ dùng giải thích nếu metadata cho phép; giao dịch phải đọc operational target."
            ]
        targets = sorted(
            [(p.get("operational_target"), p) for p in plans if p.get("operational_target") is not None],
            key=lambda x: x[0],
        )
        if not targets:
            return ["Không có target hợp lệ trong kế hoạch active."]
        if "gần" in q:
            reference = context.get("latest_completed_ohlc", {}).get("close")
            if reference is None:
                chosen = targets[0][1]
            else:
                chosen = min((p for _, p in targets), key=lambda p: abs(p["operational_target"] - reference))
            memory.focus_target_kind = "near"
            return [f"Target gần theo Close hoàn thành gần nhất là {_fmt(chosen.get('operational_target'))} của {self._plan_label(chosen)}."]
        if "xa" in q:
            reference = context.get("latest_completed_ohlc", {}).get("close")
            chosen = max((p for _, p in targets), key=lambda p: abs(p["operational_target"] - reference)) if reference is not None else targets[-1][1]
            memory.focus_target_kind = "far"
            return [f"Target xa là {_fmt(chosen.get('operational_target'))} của {self._plan_label(chosen)}."]
        lines = ["Các target active:"]
        for _, p in targets:
            lines.append(f"- {self._plan_label(p)}: {_fmt(p.get('operational_target'))} | rule {p.get('target_rule') or 'không ghi'}.")
        lines.append("Không gộp các target khác cơ chế thành một mốc giả tạo.")
        return lines

    def _answer_priority(self, context: dict[str, Any], focus_plan: dict[str, Any] | None) -> list[str]:
        plans = context.get("active_plans", [])
        operational = next((p for p in plans if p.get("engine") == "gpt_simcarrry6"), None)
        ptkt = next((p for p in plans if p.get("engine") == "gpt_simptkt"), None)
        lines: list[str] = []
        if focus_plan:
            lines.append(f"Đang tập trung vào {self._plan_label(focus_plan)} theo câu hỏi nối của khách.")
        if operational:
            lines.append("Kế hoạch operational ưu tiên là SIMCARRRY6 vì có forecast band, operational entry/target và risk action cụ thể.")
        if ptkt:
            lines.append("SIMPTKT là xác nhận PTKT độc lập; dùng để kiểm tra đồng thuận, không tự động thay SIMCARRRY6.")
        lines.append("Nếu có BLOCKER/CRITICAL chưa được mitigation thì ưu tiên an toàn của cảnh báo cao hơn thứ tự engine.")
        return lines

    def _answer_why(self, context: dict[str, Any], focus_plan: dict[str, Any] | None, question: str) -> list[str]:
        if "t+2" in question.lower():
            return [
                "t+2 là horizon xa hơn nên V50 giảm confidence/size khi metadata kích hoạt. Evidence: t+2 tăng +134,0 điểm, "
                "TRAIN +95,2, OOS1 +19,6, OOS2 +19,2 và Max DD cải thiện 13,7 điểm."
            ]
        if focus_plan:
            return [
                f"{self._plan_label(focus_plan)} dùng direction từ {focus_plan.get('direction_source') or 'metadata không ghi'}, "
                f"entry rule {focus_plan.get('entry_rule') or 'không ghi'}, target rule {focus_plan.get('target_rule') or 'không ghi'}."
            ] + self._answer_evidence(context, focus_plan, question)
        return [
            "Các engine có thể cùng direction nhưng khác target vì một bên dùng forecast band/ladder, bên kia dùng PTKT native. "
            "AI phải tách direction alpha, band alpha và execution alpha thay vì coi mọi con số là cùng một nguồn."
        ]

    def _answer_change(self, context: dict[str, Any], memory: ConversationMemory) -> list[str]:
        current = self._fingerprint(context)
        if not memory.last_context_fingerprint:
            return ["Đây là lần đầu phiên chat ghi nhận context; chưa có mốc trước để so sánh."]
        if current == memory.last_context_fingerprint:
            return ["Không có thay đổi cấu trúc trong plan, warning hoặc freshness so với lượt trước."]
        return [
            "Context đã thay đổi so với lượt trước. Tôi đã reload lại TSV/OHLC; cần đọc lại entry, target, warning và freshness mới thay vì dựa vào câu trả lời cũ."
        ]

    def _concise_live_answer(
        self,
        context: dict[str, Any],
        snapshot: SessionSnapshot,
        *,
        use_open: bool,
    ) -> list[str]:
        price = snapshot.session_open if use_open else snapshot.live_price
        label = "Open" if use_open else "Giá hiện tại"
        if price is None:
            return [f"{label}: chưa có dữ liệu hợp lệ."]
        plans = context.get("active_plans", [])
        if not plans:
            return [f"{label} {_fmt(price)}: chưa có plan forward hợp lệ; không mở lệnh mới."]
        states = self._state_map(context)
        ordered = sorted(plans, key=lambda p: (0 if p.get("engine") == "gpt_simcarrry6" else 1, p.get("horizon", "")))[:2]
        waiting_plans: list[tuple[dict[str, Any], float | None]] = []
        active_messages: list[str] = []
        prepassed = False
        for p in ordered:
            state = states.get(_plan_key(p), {})
            state_name = state.get("state", "")
            entry = p.get("operational_entry")
            distance = abs(float(entry) - float(price)) if entry is not None else None
            if state_name in {"WAIT_ENTRY", "WAIT_ENTRY_TARGET_PREPASSED"}:
                waiting_plans.append((p, distance))
            else:
                active_messages.append(f"{self._plan_label(p)}: {state.get('message') or state_name or 'đã cập nhật'}")
            prepassed = prepassed or state_name == "WAIT_ENTRY_TARGET_PREPASSED"

        lines: list[str] = []
        if waiting_plans and not active_messages:
            lines.append(f"{label} {_fmt(price)}: chưa khớp cả {len(waiting_plans)} kèo {(context.get('consensus') or {}).get('direction') or ''}.".rstrip())
            parts = [
                f"{self._plan_label(p)} entry {_fmt(p.get('operational_entry'))} (còn {_fmt(distance)})"
                for p, distance in waiting_plans
            ]
            lines.append("; ".join(parts) + ".")
            direction = (context.get("consensus") or {}).get("direction") or "lệnh"
            lines.append(f"Khuyên: WAIT_ENTRY, không {direction} đuổi.")
        else:
            lines.append(f"{label} {_fmt(price)}:")
            lines.extend(active_messages[:2])

        notes: list[str] = []
        if prepassed:
            notes.append("mốc chốt đã đi qua trước khi vùng vào được khớp; phải chạm lại sau khi có vị thế")
        if any(w.get("id") == "BAND_DIRECTION_CONTRADICTION" for w in context.get("warnings", [])):
            notes.append("cặp giá gốc đã bị loại; chỉ dùng vùng vào và mốc chốt đã sửa")
        if notes:
            lines.append("Lưu ý: " + "; ".join(notes) + ".")
        return lines

    @staticmethod
    def _compact_warning(warning: dict[str, Any]) -> str:
        wid = warning.get("id", "")
        if wid == "BAND_DIRECTION_CONTRADICTION":
            return "Cảnh báo: hai mức giá gốc nằm ngược với hướng dự báo; bỏ cặp gốc, chỉ dùng cặp đã sửa sau khi OHLC và R5 xác nhận."
        if wid in {"OPEN_TARGET_PREPASSED", "TARGET_PREPASSED_BEFORE_FILL", "INTRADAY_SEQUENCE_UNCERTAIN"}:
            return "Cảnh báo: mốc chốt xuất hiện trước khi vùng vào được khớp; chưa được tính là đã chốt lời."
        return f"Cảnh báo {warning.get('level','')}: {warning.get('title') or wid}."

    @staticmethod
    def _limit_answer_lines(lines: list[str], max_lines: int = 3) -> list[str]:
        clean = [re.sub(r"\s+", " ", str(x)).strip() for x in lines if str(x).strip()]
        return clean[:max_lines]

    @staticmethod
    def _primary_plan(context: dict[str, Any]) -> dict[str, Any] | None:
        plans = context.get("active_plans", [])
        preferred = [p for p in plans if p.get("engine") == "gpt_simcarrry6" and p.get("horizon") == "t+1"]
        if preferred:
            return preferred[0]
        preferred = [p for p in plans if p.get("engine") == "gpt_simcarrry6"]
        return preferred[0] if preferred else (plans[0] if plans else None)

    def _focused_live_answer(self, context: dict[str, Any], snapshot: SessionSnapshot) -> list[str]:
        price = snapshot.live_price if snapshot.live_price is not None else snapshot.session_open
        if price is None:
            return ["Chưa có giá hiện tại để đánh giá; tạm thời không mở lệnh mới."]
        plans = context.get("active_plans", [])
        if not plans:
            return [f"Giá {_fmt(price)}: chưa có kèo mới hợp lệ; đứng ngoài."]
        primary = self._primary_plan(context)
        states = self._state_map(context)
        direction = (context.get("consensus") or {}).get("direction") or (primary or {}).get("direction") or ""
        label = f"Giá {_fmt(price)}"
        if snapshot.session_open is not None:
            label += f" (mở cửa {_fmt(snapshot.session_open)})"
        ordered = [primary] + [p for p in plans if p is not primary] if primary else plans
        evaluated = [(p, states.get(_plan_key(p), {})) for p in ordered if p]
        waiting = [(p, st) for p, st in evaluated if st.get("state") in {"WAIT_ENTRY", "WAIT_ENTRY_TARGET_PREPASSED"}]
        if waiting and len(waiting) == len(evaluated):
            entry = primary.get("operational_entry") if primary else None
            lines = [f"{label}: chưa khớp kèo {direction}."]
            if direction == "SHORT" and entry is not None and float(price) < float(entry):
                wait_text = f"chờ hồi lên {_fmt(entry)}"
            elif direction == "LONG" and entry is not None and float(price) > float(entry):
                wait_text = f"chờ điều chỉnh xuống {_fmt(entry)}"
            else:
                wait_text = f"chờ giá chạm {_fmt(entry)}"
            lines.append(f"Khuyên: đứng ngoài, {wait_text}; không {direction} đuổi.")
            primary_state = states.get(_plan_key(primary), {}) if primary else {}
            primary_target = primary.get("operational_target") if primary else None
            primary_entry = primary.get("operational_entry") if primary else None
            target_prepassed_now = bool(
                primary and primary_target is not None and primary_entry is not None and (
                    (primary.get("direction") == "SHORT" and float(price) <= float(primary_target) < float(primary_entry))
                    or (primary.get("direction") == "LONG" and float(price) >= float(primary_target) > float(primary_entry))
                )
            )
            if primary_state.get("state") == "WAIT_ENTRY_TARGET_PREPASSED" or target_prepassed_now:
                lines.append(f"Mốc chốt {_fmt(primary_target)} đã đi qua trước khi vào lệnh, nên chưa được tính chốt lời.")
            elif any(w.get("id") == "BAND_DIRECTION_CONTRADICTION" for w in context.get("warnings", [])):
                lines.append("Kèo chỉ dùng các mức vào/chốt đã được hệ hiệu chỉnh.")
            return self._limit_answer_lines(lines)
        # Khi đã có fill hoặc trạng thái khác, chỉ nói trạng thái chính và bước tiếp theo.
        p, st = evaluated[0]
        state_name = st.get("state", "")
        if state_name == "TARGET_HIT":
            return self._limit_answer_lines([
                f"{label}: kèo {p.get('direction')} đã chạm mục tiêu {_fmt(p.get('operational_target'))}.",
                "Không mở thêm lệnh mới chỉ vì mục tiêu vừa đạt.",
            ])
        if state_name in {"FILLED_ACTIVE", "FILLED_SEQUENCE_UNCERTAIN"}:
            lines = [f"{label}: kèo {p.get('direction')} đã chạm vùng vào {_fmt(p.get('operational_entry'))}."]
            lines.append(f"Theo dõi mốc {_fmt(p.get('operational_target'))}; chưa kết luận lời nếu chưa rõ thứ tự giá trong phiên.")
            return self._limit_answer_lines(lines)
        return self._limit_answer_lines([f"{label}: {st.get('message') or 'trạng thái đã được cập nhật' }."])

    @staticmethod
    def _volume_rule_details(plan: dict[str, Any]) -> dict[str, float | None]:
        rule = str(plan.get("volume_rule") or "")
        out: dict[str, float | None] = {"base": None, "add": None, "step": None, "max": None}
        m = re.search(r"BASE_([0-9.]+)_ADD_([0-9.]+)_PER_([0-9.]+)PT_MAX_([0-9.]+)", rule, flags=re.IGNORECASE)
        if m:
            out.update(base=float(m.group(1)), add=float(m.group(2)), step=float(m.group(3)), max=float(m.group(4)))
            return out
        cap = re.search(r"CAP\s*([0-9.]+)", str(plan.get("profile") or ""), flags=re.IGNORECASE)
        if cap:
            out["max"] = float(cap.group(1))
        return out

    def _volume_rule_line(self, plan: dict[str, Any]) -> str:
        d = self._volume_rule_details(plan)
        risk = str(plan.get("risk_action") or "").upper()
        if d.get("base") is not None and d.get("add") is not None and d.get("step") is not None and d.get("max") is not None:
            action = "được chạy đủ ladder sau khi fill hợp lệ" if "FULL" in risk else ("chỉ chạy nửa ladder sau khi fill hợp lệ" if "HALF" in risk else "chỉ kích hoạt sau khi fill hợp lệ")
            return (
                f"Khối lượng: vào nền {_fmt(d['base'],2)}, thêm {_fmt(d['add'],2)} mỗi {_fmt(d['step'],1)} điểm theo ladder của engine, "
                f"tổng tối đa {_fmt(d['max'],2)}; {action}, không phải vào toàn bộ ngay một lệnh."
            )
        if d.get("max") is not None:
            return f"Khối lượng tối đa của profile này là {_fmt(d['max'],2)}; outputs hiện không ghi chi tiết từng nấc nên không tự bịa cách chia lệnh."
        if plan.get("units") is not None:
            return f"Khối lượng hiện ghi trong outputs: {_fmt(plan.get('units'),2)} units."
        return "Outputs hiện không ghi volume rule cho plan này; không được lấy rule của engine khác để trả thay."

    def _compact_volume_text(self, plan: dict[str, Any]) -> str:
        d = self._volume_rule_details(plan)
        if all(d.get(k) is not None for k in ("base", "add", "step", "max")):
            return f"size nền {_fmt(d['base'],2)}, cộng {_fmt(d['add'],2)}/{_fmt(d['step'],1)} điểm, tối đa {_fmt(d['max'],2)}"
        if d.get("max") is not None:
            return f"size tối đa {_fmt(d['max'],2)}"
        if plan.get("units") is not None:
            return f"size {_fmt(plan.get('units'),2)} units"
        return "size chưa được outputs ghi rõ"

    @staticmethod
    def _activation_scenario_line(plan: dict[str, Any]) -> str:
        side = str(plan.get("direction") or "").upper()
        entry = plan.get("operational_entry")
        target = plan.get("operational_target")
        if side == "SHORT":
            return (
                f"Kịch bản đúng: High chưa chạm {_fmt(entry)} thì chờ; High chạm/xuyên rồi giá live quay xuống dưới {_fmt(entry)} thì SHORT được kích hoạt; "
                f"giá còn giữ trên {_fmt(entry)} thì chưa SHORT. Mốc {_fmt(target)} chỉ là target sau khi đã fill, đi qua trước fill không tính thắng."
            )
        if side == "LONG":
            return (
                f"Kịch bản đúng: Low chưa chạm {_fmt(entry)} thì chờ; Low chạm/xuyên rồi giá live lấy lại trên {_fmt(entry)} thì LONG được kích hoạt; "
                f"giá còn giữ dưới {_fmt(entry)} thì chưa LONG. Mốc {_fmt(target)} chỉ là target sau khi đã fill, đi qua trước fill không tính thắng."
            )
        return "Outputs chưa có hướng LONG/SHORT rõ để dựng kịch bản kích hoạt."

    def _available_engine_summary(self, context: dict[str, Any]) -> str:
        labels: list[str] = []
        for p in context.get("active_plans", []):
            label = self._plan_label(p)
            if label and label not in labels:
                labels.append(label)
        return ", ".join(labels) if labels else "không có engine forward hợp lệ"

    def _r5_execution_authority_line(self, context: dict[str, Any], system_side: str) -> str:
        r5 = self._r5_control(context)
        action = str(r5.get("current_action") or "NO_SIGNAL").upper()
        cap = r5.get("max_position")
        opposite = "LONG" if system_side == "SHORT" else "SHORT"
        if action == "KEEP":
            return f"Quyền R5: KEEP — được thực thi kèo {system_side} theo ladder của engine; không tự đảo {opposite}."
        if action == "CANCEL":
            return "Quyền R5: CANCEL — units 0, không thực thi kèo thuận và không mở kèo gỡ ngược chiều."
        if action == "FLIP_HINT":
            return f"Quyền R5: FLIP_HINT — không chạy kèo {system_side} cũ; chỉ xét {opposite} có OHLC xác nhận, khởi đầu 0,10 và tối đa {_fmt(cap,2) if cap is not None else 'theo cap outputs'}."
        if action == "PRE_OPEN":
            return f"Quyền R5: PRE_OPEN — mới là bản đồ; kể cả khi giá sau đó thỏa điều kiện fill của {system_side}, vẫn phải chờ outputs chốt KEEP/CANCEL/FLIP_HINT trước khi coi là lệnh cuối."
        return "Quyền R5 chưa rõ trong outputs; chỉ mô tả plan, chưa nâng thành lệnh cuối."

    @staticmethod
    def _plain_original_pair_explanation(plan: dict[str, Any], *, mention_internal_label: bool = False) -> str:
        side = str(plan.get("direction") or "").upper()
        ref = plan.get("original_entry")
        expected = plan.get("original_target")
        op_entry = plan.get("operational_entry")
        op_target = plan.get("operational_target")
        label = " Nhãn nội bộ của nhóm này là V44." if mention_internal_label else ""
        if side == "SHORT" and ref is not None and expected is not None:
            return (
                f"Cảnh báo cụ thể: bản tính gốc lấy {_fmt(ref)} làm giá tham chiếu nhưng lại gọi {_fmt(expected)} là đáy kỳ vọng. "
                f"Với một kèo SHORT, đáy kỳ vọng phải nằm thấp hơn giá tham chiếu; ở đây nó nằm cao hơn nên cặp giá gốc tự mâu thuẫn.{label} "
                f"Hệ đã sửa vai trò thành chờ SHORT {_fmt(op_entry)} rồi chốt {_fmt(op_target)}."
            )
        if side == "LONG" and ref is not None and expected is not None:
            return (
                f"Cảnh báo cụ thể: bản tính gốc lấy {_fmt(ref)} làm giá tham chiếu nhưng lại gọi {_fmt(expected)} là đỉnh kỳ vọng. "
                f"Với một kèo LONG, đỉnh kỳ vọng phải nằm cao hơn giá tham chiếu; ở đây nó nằm thấp hơn nên cặp giá gốc tự mâu thuẫn.{label} "
                f"Hệ đã sửa vai trò thành chờ LONG {_fmt(op_entry)} rồi chốt {_fmt(op_target)}."
            )
        return f"Cặp giá gốc không khớp với hướng dự báo; chỉ dùng cặp vận hành {_fmt(op_entry)} → {_fmt(op_target)}.{label}"

    @staticmethod
    def _snapshot_line(snapshot: SessionSnapshot) -> str:
        parts = []
        if snapshot.session_open is not None:
            parts.append(f"O {_fmt(snapshot.session_open)}")
        if snapshot.session_high is not None:
            parts.append(f"H {_fmt(snapshot.session_high)}")
        if snapshot.session_low is not None:
            parts.append(f"L {_fmt(snapshot.session_low)}")
        if snapshot.session_close is not None:
            parts.append(f"C {_fmt(snapshot.session_close)}")
        elif snapshot.live_price is not None:
            parts.append(f"P {_fmt(snapshot.live_price)}")
        prefix = "Dòng OHLC khách dán" if snapshot.input_source in {"header_table", "date_row"} else "OHLC khách cung cấp"
        return prefix + ": " + " | ".join(parts) + "." if parts else ""

    def _engine_playbook_lines(
        self,
        context: dict[str, Any],
        plan: dict[str, Any] | None,
        requested_engine: str,
        requested_profile: str,
        snapshot: SessionSnapshot,
    ) -> list[str]:
        if not plan:
            name = "SimPTKT" if requested_engine == "gpt_simptkt" else ("SimCarry" if requested_engine == "gpt_simcarrry6" else (requested_profile or requested_engine or "engine được hỏi"))
            return [
                f"Outputs hôm nay không có plan active của {name}; vì vậy không có direction, entry, target hay khối lượng hợp lệ để mô tả.",
                f"Các plan đang thực sự có: {self._available_engine_summary(context)}.",
                f"Tao không lấy kèo của engine khác để trả thay cho {name}.",
            ]
        side = str(plan.get("direction") or "NA").upper()
        entry = plan.get("operational_entry")
        target = plan.get("operational_target")
        phase = str(plan.get("phase") or "").upper()
        label = self._plan_label(plan)
        lines = [f"{label}: kèo {side}, vùng vào {_fmt(entry)} → mốc chốt {_fmt(target)}; trạng thái {phase or plan.get('freshness_status') or 'NA'}." ]
        lines.append(self._volume_rule_line(plan))
        lines.append(self._r5_execution_authority_line(context, side))
        if plan.get("entry_target_swap_applied"):
            lines.append(self._plain_original_pair_explanation(plan))
        if snapshot.live_price is not None:
            snap_line = self._snapshot_line(snapshot)
            if snap_line:
                lines.append(snap_line)
            action = self._intraday_action_lines(context, plan, snapshot, requested_side=side)
            if action:
                lines.extend(action[:2])
        else:
            lines.append(self._activation_scenario_line(plan))
        if plan.get("engine") == "engine5":
            r5 = self._r5_control(context)
            lines.append(f"R5 hiện {r5.get('current_action') or 'NO_SIGNAL'}: PRE_OPEN chỉ dựng kịch bản; KEEP/CANCEL/FLIP_HINT mới quyết định quyền giao dịch sau Open.")
        return self._limit_answer_lines(lines, max_lines=7)

    def _system_playbook_lines(self, context: dict[str, Any], snapshot: SessionSnapshot) -> list[str]:
        plans = context.get("active_plans", [])
        primary = self._primary_plan(context)
        if not plans or not primary:
            return ["Hôm nay chưa có plan forward hợp lệ trong outputs; không phát kèo."]
        lines: list[str] = []
        lines.append(
            f"Kèo chính hôm nay: {self._plan_label(primary)} {str(primary.get('direction') or '').upper()} {_fmt(primary.get('operational_entry'))} → {_fmt(primary.get('operational_target'))}."
        )
        lines.append(self._volume_rule_line(primary))
        engine5 = [p for p in plans if p.get("engine") == "engine5"]
        if engine5:
            maps = "; ".join(f"{p.get('profile')}: {_fmt(p.get('operational_entry'))} → {_fmt(p.get('operational_target'))}" for p in engine5)
            r5 = self._r5_control(context)
            lines.append(f"R5/Engine5: {maps}; " + self._r5_execution_authority_line(context, str(primary.get("direction") or "").upper()))
        has_ptkt = any(p.get("engine") == "gpt_simptkt" for p in plans)
        if not has_ptkt:
            lines.append("SimPTKT: không có row active trong outputs hôm nay, nên không được gán kèo hoặc khối lượng cho SimPTKT.")
        if primary.get("entry_target_swap_applied"):
            lines.append(self._plain_original_pair_explanation(primary))
        if snapshot.live_price is not None:
            snap_line = self._snapshot_line(snapshot)
            if snap_line:
                lines.append(snap_line)
            action = self._intraday_action_lines(context, primary, snapshot, requested_side=str(primary.get("direction") or "").upper())
            if action:
                lines.extend(action[:3])
        else:
            lines.append(self._activation_scenario_line(primary))
            lines.append("Gửi O–H–L–P để hệ đối chiếu phiên đang nằm ở nhánh nào; không có OHLC thì chưa được biến bản đồ thành lệnh vào ngay.")
        return self._limit_answer_lines(lines, max_lines=7)

    def _focused_current_plan(self, context: dict[str, Any]) -> list[str]:
        primary = self._primary_plan(context)
        if not primary:
            return ["Chưa có kèo forward hợp lệ; đứng ngoài."]
        con = context.get("consensus") or {}
        direction = con.get("direction") or primary.get("direction")
        lines = [f"Kèo chính đang dùng: {direction}, chờ vào {_fmt(primary.get('operational_entry'))}, chốt {_fmt(primary.get('operational_target'))}."]
        if con.get("is_unanimous"):
            lines.append("Các hệ đang cùng hướng, nhưng đồng thuận không thay thế điều kiện giá: vẫn phải chạm entry và xác nhận đúng kiểu rejection/reclaim.")
        severe = self._severe_warnings(context)
        if severe:
            if any(w.get("id") == "BAND_DIRECTION_CONTRADICTION" for w in severe):
                lines.append("Lưu ý: hai mức giá gốc nằm ngược với hướng dự báo nên đã bị loại; kế hoạch trên là cặp vào–ra đã được hệ sửa lại.")
            else:
                lines.append("Có cảnh báo mạnh đang hoạt động; đọc điều kiện cụ thể trước khi vào, không chỉ nhìn direction.")
        return self._limit_answer_lines(lines)

    def _forecast_levels(self, context: dict[str, Any]) -> dict[str, Any]:
        plans = context.get("active_plans", [])
        levels: list[float] = []
        entries: list[float] = []
        targets: list[float] = []
        forecasts: list[float] = []
        for plan in plans:
            for key, bucket in (("operational_entry", entries), ("operational_target", targets), ("forecast", forecasts)):
                value = plan.get(key)
                try:
                    if value is not None:
                        v = float(value)
                        bucket.append(v)
                        levels.append(v)
                except (TypeError, ValueError):
                    pass
        return {
            "all": sorted(set(levels)),
            "entries": sorted(set(entries)),
            "targets": sorted(set(targets)),
            "forecasts": sorted(set(forecasts)),
        }

    def _focused_forecast_range(
        self,
        context: dict[str, Any],
        snapshot: SessionSnapshot | None = None,
        question: str = "",
        focus_plan: dict[str, Any] | None = None,
    ) -> list[str]:
        """Explain realized intraday range and the forecast band encoded by one plan.

        A realized range comes directly from the user's current H/L. A forecast band may
        be encoded by ForecastSignal/ExpectedLow (or by the original source pair) inside
        one engine and one horizon. We never merge levels across plans, but we also do
        not require literal expected_high/expected_low column names when the TSV metadata
        clearly identifies an equivalent same-plan band.
        """
        q = self._norm(question)
        asks_realized = any(x in q for x in ["biên mới", "biên hiện tại", "biên thực tế", "range hiện tại", "đang chạy biên"])
        asks_forecast = any(x in q for x in ["dự kiến", "dự báo", "forecast", "expected", "high low dự kiến"])
        lines: list[str] = []

        if snapshot is not None and snapshot.session_high is not None and snapshot.session_low is not None and (asks_realized or not asks_forecast):
            hi_now = float(snapshot.session_high)
            lo_now = float(snapshot.session_low)
            width = hi_now - lo_now
            lines.append(f"Biên thực tế vừa cập nhật: Low {_fmt(lo_now)} – High {_fmt(hi_now)}, rộng {_fmt(width)} điểm.")
            if snapshot.live_price is not None:
                p = float(snapshot.live_price)
                pos = ((p - lo_now) / width) if width > 0 else 0.5
                lines.append(f"Giá hiện {_fmt(p)}: cách Low {_fmt(p-lo_now)} điểm, cách High {_fmt(hi_now-p)} điểm, nằm khoảng {pos:.0%} biên phiên.")
            if snapshot.session_open is not None and snapshot.live_price is not None:
                delta = float(snapshot.live_price) - float(snapshot.session_open)
                lines.append(f"So với Open {_fmt(snapshot.session_open)}, giá đang {('cao hơn' if delta >= 0 else 'thấp hơn')} {_fmt(abs(delta))} điểm.")

        plans = context.get("active_plans", [])
        ordered = ([focus_plan] if focus_plan else []) + [p for p in plans if p is not focus_plan]
        band = None
        for plan in ordered:
            if not plan:
                continue
            lo = plan.get("expected_low")
            hi = plan.get("expected_high")
            source_kind = "ExpectedLow/ExpectedHigh"
            try:
                if lo is not None and hi is not None and float(lo) <= float(hi):
                    band = (float(lo), float(hi), plan, source_kind)
                    break
            except (TypeError, ValueError):
                pass

            entry_rule = str(plan.get("entry_rule") or "")
            target_rule = str(plan.get("target_rule") or "")
            raw_a = plan.get("original_entry")
            raw_b = plan.get("original_target")
            forecast = plan.get("forecast")
            try:
                # SIMCARRRY6 T+2 writes ForecastSignal as the upper forecast and
                # ExpectedLow as the lower forecast in the same row/horizon.
                if "ForecastSignal" in entry_rule and "ExpectedLow" in target_rule:
                    hi_f = float(raw_a if raw_a is not None else forecast)
                    lo_f = float(raw_b)
                    band = (min(lo_f, hi_f), max(lo_f, hi_f), plan, "ForecastSignal/ExpectedLow")
                    break
                # Other SIM rows still carry a same-plan source span in OriginalEntry/
                # OriginalTarget. Report it as an encoded forecast span, not as a
                # literal Expected High/Low pair.
                if plan.get("engine") == "gpt_simcarrry6" and raw_a is not None and raw_b is not None:
                    a, b = float(raw_a), float(raw_b)
                    band = (min(a, b), max(a, b), plan, "source forecast span")
                    break
            except (TypeError, ValueError):
                continue

        if band is not None:
            lo, hi, source, source_kind = band
            lines.append(f"Biên dự kiến của đúng {self._plan_label(source)}: {_fmt(lo)} – {_fmt(hi)}, rộng {_fmt(hi-lo)} điểm.")
            lines.append(f"Nguồn cùng một row/horizon: {source_kind}; không ghép với engine khác hay T+ khác.")
            if snapshot is not None and snapshot.live_price is not None:
                p = float(snapshot.live_price)
                if p < lo:
                    lines.append(f"Giá {_fmt(p)} đang thấp hơn đáy band {_fmt(lo)} {_fmt(lo-p)} điểm: band đã bị xuyên xuống, phải coi là lệch/kém hiệu lực chứ không dùng máy móc.")
                elif p > hi:
                    lines.append(f"Giá {_fmt(p)} đang cao hơn đỉnh band {_fmt(hi)} {_fmt(p-hi)} điểm: band đã bị xuyên lên, phải coi là lệch/kém hiệu lực.")
                else:
                    lines.append(f"Trong band dự kiến, giá còn {_fmt(hi-p)} điểm lên mép trên và {_fmt(p-lo)} điểm xuống mép dưới.")
            return self._limit_answer_lines(lines, max_lines=6)

        if lines:
            lines.append("Outputs chưa mã hóa được forecast band cho đúng engine/horizon đang xét; chỉ có thể báo biên thực tế H–L vừa cung cấp.")
            return self._limit_answer_lines(lines, max_lines=5)
        return ["Chưa có High–Low thực tế và cũng chưa đọc được forecast band hợp lệ trong cùng một engine/horizon; chưa thể trả biên."]

    def _focused_forecast_chart(self, context: dict[str, Any]) -> list[str]:
        primary = self._primary_plan(context)
        lv = self._forecast_levels(context)
        if not primary or not lv["all"]:
            return ["Chưa có dữ liệu forecast hợp lệ trong outputs để dựng chart."]
        direction = str(primary.get("direction") or "").upper()
        entry = primary.get("operational_entry")
        target = primary.get("operational_target")
        low, high = lv["all"][0], lv["all"][-1]
        ladder = " → ".join(_fmt(x) for x in lv["all"][:6])
        return self._limit_answer_lines([
            f"Chart forecast dạng thang giá: {ladder}.",
            f"Khung tổng {_fmt(low)}–{_fmt(high)}; kèo frozen {direction}: chờ {_fmt(entry)} → {_fmt(target)}.",
            "Web sẽ dựng PNG thật bằng Python/Matplotlib từ ba database rootless; không gọi ứng dụng tạo ảnh.",
        ], max_lines=4)

    def _focused_evidence(self, context: dict[str, Any], focus_plan: dict[str, Any] | None) -> list[str]:
        warnings = self._warning_for_plan(context, focus_plan) if focus_plan else context.get("warnings", [])
        contradiction = next((w for w in warnings if w.get("id") == "BAND_DIRECTION_CONTRADICTION"), None)
        if not contradiction:
            return ["Kèo hiện tại không kích hoạt cảnh báo backtest trọng yếu trong catalog."]
        plan = self._warning_plan(context, contradiction, focus_plan)
        direction = str(contradiction.get("direction") or (plan or {}).get("direction") or "NA").upper()
        raw_entry = (plan or {}).get("original_entry")
        raw_target = (plan or {}).get("original_target")
        if direction == "SHORT":
            lines = [f"Điều kiện bị cảnh báo: hệ gốc nói SHORT nhưng mức đáy kỳ vọng {_fmt(raw_target)} lại cao hơn giá tham chiếu {_fmt(raw_entry)}; cặp giá gốc bị đảo nên không được dùng đặt lệnh."]
        elif direction == "LONG":
            lines = [f"Điều kiện bị cảnh báo: hệ gốc nói LONG nhưng mức đỉnh kỳ vọng {_fmt(raw_target)} lại thấp hơn giá tham chiếu {_fmt(raw_entry)}; cặp giá gốc bị đảo nên không được dùng đặt lệnh."]
        else:
            lines = ["Điều kiện bị cảnh báo: hướng dự báo và hai mức giá gốc nằm sai phía nhau; không dùng cặp gốc đặt lệnh."]
        metrics = contradiction.get("evidence_metrics") or contradiction.get("backtest_metrics") or {}
        full = metrics.get("full", {})
        splits = metrics.get("splits", [])
        if full:
            lines.append(f"Toàn kỳ 2018–2026: {int(full.get('raw_touched_trades', 0))} giao dịch theo cặp gốc, WR {full.get('raw_wr_pct', 0):.1f}%, PnL {full.get('raw_pnl_points', 0):+.1f} điểm.")
            names = {
                "TRAIN 2018-2022": "giai đoạn xây dựng 2018–2022",
                "OOS1 2023-2024": "kiểm tra độc lập 2023–2024",
                "OOS2 2025-2026": "kiểm tra độc lập 2025–2026",
            }
            raw_parts = []
            swap_parts = []
            for row in splits:
                name = names.get(str(row.get("period")), str(row.get("period")))
                if row.get("raw_pnl_points") is not None:
                    raw_parts.append(f"{name}: {float(row['raw_pnl_points']):+.1f}")
                if row.get("swap_operational_pnl_points") is not None:
                    swap_parts.append(f"{name}: {float(row['swap_operational_pnl_points']):+.1f}")
            if raw_parts:
                lines.append("Cặp gốc: " + "; ".join(raw_parts) + " điểm.")
            if full.get("swap_operational_pnl_points") is not None:
                lines.append(f"Sau khi sửa đúng vai trò entry–target: tổng {full.get('swap_operational_pnl_points', 0):+.1f} điểm; " + "; ".join(swap_parts) + " điểm.")
        if plan:
            lines.append(f"Áp dụng hôm nay: bỏ cặp gốc; chỉ dùng {direction} đã sửa {_fmt(plan.get('operational_entry'))} → {_fmt(plan.get('operational_target'))}, đúng thứ tự khớp lệnh và theo quyền quyết định của R5.")
        return self._limit_answer_lines(lines, max_lines=6)

    def _load_bridge_long_audit(self) -> dict[str, Any]:
        candidates = [
            self.trades_path.parent / "BRIDGE_LONG_TO_SHORT_ENTRY_AUDIT.json",
            Path(__file__).resolve().parents[1] / "outputs" / "BRIDGE_LONG_TO_SHORT_ENTRY_AUDIT.json",
            Path(__file__).resolve().parent / "config" / "bridge_long_policy.json",
        ]
        for path in candidates:
            try:
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["_path"] = str(path)
                    return data
            except Exception:
                continue
        return {"status": "NOT_BACKTESTED", "promoted": False}

    def _bridge_basis_levels(self, plan: dict[str, Any]) -> tuple[float | None, float | None]:
        """Return completed basis Close and prior ExpectedLow from the same SIMCARRRY6 lineage."""
        try:
            df = pd.read_csv(self.trades_path, sep="\t", dtype=str).fillna("")
            labels = df.get("EngineChartLabel", pd.Series(dtype=str)).astype(str).str.lower()
            kinds = df.get("RowKind", pd.Series(dtype=str)).astype(str).str.upper()
            hist = df[labels.str.contains("simcarrry6", na=False) & kinds.eq("HISTORY")].copy()
            if hist.empty:
                return None, None
            hist["_date"] = pd.to_datetime(hist.get("SortDate", hist.get("Ngày entry", "")), errors="coerce")
            plan_date = pd.to_datetime(plan.get("date"), dayfirst=True, errors="coerce")
            if pd.notna(plan_date):
                hist = hist[hist["_date"] < plan_date]
            hist = hist.sort_values("_date", ascending=False)
            row = hist.iloc[0]
            note = str(row.get("Ghi chú HybridV3", ""))
            close_m = re.search(r"(?:^|[/;])C(?:lose)?\s*=?\s*(-?\d+(?:\.\d+)?)", note, re.I)
            prev_close = float(close_m.group(1)) if close_m else None
            exp_m = re.search(r"(?:OriginalTarget|OperationalTarget)\s*=\s*(-?\d+(?:\.\d+)?)", note, re.I)
            expected_low = float(exp_m.group(1)) if exp_m else None
            if expected_low is None:
                try:
                    expected_low = float(str(row.get("Exit", "")).replace(",", "."))
                except Exception:
                    pass
            return prev_close, expected_low
        except Exception:
            return None, None

    def _answer_bridge_long_to_short(
        self, context: dict[str, Any], focus_plan: dict[str, Any] | None, snapshot: SessionSnapshot
    ) -> list[str]:
        plan = focus_plan or self._primary_plan(context)
        if not plan or str(plan.get("direction") or "").upper() != "SHORT" or str(plan.get("horizon") or "").lower() != "t+1":
            return ["LONG reclaim chỉ áp dụng khi database có kèo SIMCARRRY6 SHORT t+1; hiện chưa có kèo phù hợp."]
        audit = self._load_bridge_long_audit()
        promoted = bool(audit.get("promoted")) and str(audit.get("status", "")).upper() in {"PASS", "PROMOTED"}
        short_entry = plan.get("operational_entry")
        price = snapshot.live_price
        prev_close, expected_low = self._bridge_basis_levels(plan)
        stop_candidates = [float(x) for x in (prev_close, expected_low) if x is not None]
        lower = min(stop_candidates) if stop_candidates else None
        buffer_pts = float(audit.get("reclaim_buffer_points", 1.0))
        trigger = lower + buffer_pts if lower is not None else None
        max_size = float(audit.get("max_size", 0.30))
        if not promoted:
            status = str(audit.get("status") or "NOT_BACKTESTED")
            return [
                f"LONG reclaim tới vùng SHORT {_fmt(short_entry)} chưa được phép vì audit đang {status}.",
                f"Điều kiện nghiên cứu: Open ≤ Lower; chờ vượt Lower+{buffer_pts:.1f}; TP {_fmt(short_entry)}, SL Lower, đóng ATC nếu chưa TP/SL.",
            ]
        if lower is None or trigger is None or short_entry is None:
            return ["Không dựng được LONG reclaim vì database thiếu Close basis hoặc Expected Low của phiên trước; không tự đoán mức."]
        lines = [
            f"Kịch bản LONG reclaim đã backtest PASS: Lower/SL {_fmt(lower)} = min(Close basis {_fmt(prev_close)}, Expected Low trước {_fmt(expected_low)}); điểm xác nhận LONG {_fmt(trigger)}.",
            f"TP tại entry SHORT t+1 {_fmt(short_entry)}; tối đa {max_size:.2f} vị thế; chưa TP/SL thì đóng ATC.",
        ]
        op = snapshot.session_open
        hi = snapshot.session_high
        if op is None:
            lines.append("Chưa có Open nên chưa biết phiên này có đủ điều kiện Open ≤ Lower; gửi O–H–L–P để kích hoạt đúng rule.")
        elif float(op) > lower:
            lines.append(f"Không kích hoạt: Open {_fmt(op)} cao hơn Lower {_fmt(lower)}. Rule này không cho LONG đuổi khi không có gap xuống dưới Lower.")
        elif price is None:
            lines.append(f"Open {_fmt(op)} đủ điều kiện; nhưng chưa có giá hiện tại. Chỉ LONG sau khi giá vượt {_fmt(trigger)}, không bắt đáy dưới trigger.")
        elif float(price) >= float(short_entry):
            lines.append(f"Giá {_fmt(price)} đã tới/vượt TP {_fmt(short_entry)}: không mở LONG mới; chuyển sang kiểm tra kèo SHORT chính.")
        elif float(price) < trigger:
            if hi is not None and float(hi) >= trigger and float(price) <= lower:
                lines.append(f"Nhịp reclaim đã thất bại: High từng vượt {_fmt(trigger)} nhưng giá mất lại Lower {_fmt(lower)}; đóng/không mở LONG.")
            else:
                lines.append(f"Chưa LONG: giá {_fmt(price)} còn dưới điểm reclaim {_fmt(trigger)}. Không bắt đáy trước xác nhận.")
        else:
            lines.append(f"Đã reclaim: giá {_fmt(price)} ≥ {_fmt(trigger)} và còn dưới TP {_fmt(short_entry)}; có thể LONG scalp, SL {_fmt(lower)}, TP {_fmt(short_entry)}, size tối đa {max_size:.2f}.")
        metrics = audit.get("full", {})
        if metrics:
            lines.append(f"Audit 2018–2026: {int(metrics.get('trades',0))} lệnh, WR {float(metrics.get('wr_pct',0)):.2f}%, PnL sau phí {float(metrics.get('pnl_points',0)):+.2f}, MaxDD {float(metrics.get('max_dd_points',0)):.2f} điểm.")
        return self._limit_answer_lines(lines, max_lines=6)

    def _load_bridge_short_audit(self) -> dict[str, Any]:
        candidates = [
            self.trades_path.parent / "BRIDGE_SHORT_TO_LONG_ENTRY_AUDIT.json",
            Path(__file__).resolve().parents[1] / "outputs" / "BRIDGE_SHORT_TO_LONG_ENTRY_AUDIT.json",
            Path(__file__).resolve().parent / "config" / "bridge_short_policy.json",
        ]
        for path in candidates:
            try:
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["_path"] = str(path)
                    return data
            except Exception:
                continue
        return {"status": "NOT_BACKTESTED", "promoted": False}

    def _bridge_short_basis_levels(self, plan: dict[str, Any]) -> tuple[float | None, float | None]:
        """Return completed basis Close and prior ExpectedHigh from the same SIMCARRRY6 lineage."""
        try:
            df = pd.read_csv(self.trades_path, sep="\t", dtype=str).fillna("")
            labels = df.get("EngineChartLabel", pd.Series(dtype=str)).astype(str).str.lower()
            kinds = df.get("RowKind", pd.Series(dtype=str)).astype(str).str.upper()
            hist = df[labels.str.contains("simcarrry6", na=False) & kinds.eq("HISTORY")].copy()
            if hist.empty:
                return None, None
            hist["_date"] = pd.to_datetime(hist.get("SortDate", hist.get("Ngày entry", "")), errors="coerce")
            plan_date = pd.to_datetime(plan.get("date"), dayfirst=True, errors="coerce")
            if pd.notna(plan_date):
                hist = hist[hist["_date"] < plan_date]
            hist = hist.sort_values("_date", ascending=False)
            row = hist.iloc[0]
            note = str(row.get("Ghi chú HybridV3", ""))
            close_m = re.search(r"(?:^|[/;])C(?:lose)?\s*=?\s*(-?\d+(?:\.\d+)?)", note, re.I)
            prev_close = float(close_m.group(1)) if close_m else None
            exp_m = re.search(r"(?:OriginalTarget|OperationalTarget)\s*=\s*(-?\d+(?:\.\d+)?)", note, re.I)
            expected_high = float(exp_m.group(1)) if exp_m else None
            if expected_high is None:
                try:
                    expected_high = float(str(row.get("Exit", "")).replace(",", "."))
                except Exception:
                    pass
            return prev_close, expected_high
        except Exception:
            return None, None

    def _answer_bridge_short_to_long(
        self, context: dict[str, Any], focus_plan: dict[str, Any] | None, snapshot: SessionSnapshot
    ) -> list[str]:
        plan = focus_plan or self._primary_plan(context)
        if not plan or str(plan.get("direction") or "").upper() != "LONG" or str(plan.get("horizon") or "").lower() != "t+1":
            return ["SHORT reclaim chỉ áp dụng khi database có kèo SIMCARRRY6 LONG t+1; hiện chưa có kèo phù hợp."]
        audit = self._load_bridge_short_audit()
        promoted = bool(audit.get("promoted")) and str(audit.get("status", "")).upper() in {"PASS", "PROMOTED"}
        long_entry = plan.get("operational_entry")
        price = snapshot.live_price
        prev_close, expected_high = self._bridge_short_basis_levels(plan)
        upper_candidates = [float(x) for x in (prev_close, expected_high) if x is not None]
        upper = max(upper_candidates) if upper_candidates else None
        buffer_pts = float(audit.get("reclaim_buffer_points", 1.0))
        trigger = upper - buffer_pts if upper is not None else None
        max_size = float(audit.get("max_size", 0.30))
        if not promoted:
            status = str(audit.get("status") or "NOT_BACKTESTED")
            return [
                f"SHORT reclaim tới vùng LONG {_fmt(long_entry)} chưa được phép vì audit đang {status}.",
                f"Điều kiện nghiên cứu: Open ≥ Upper; chờ rơi dưới Upper-{buffer_pts:.1f}; TP {_fmt(long_entry)}, SL Upper, đóng ATC nếu chưa TP/SL.",
            ]
        if upper is None or trigger is None or long_entry is None:
            return ["Không dựng được SHORT reclaim vì database thiếu Close basis hoặc Expected High của phiên trước; không tự đoán mức."]
        lines = [
            f"Kịch bản SHORT reclaim đã backtest PASS: Upper/SL {_fmt(upper)} = max(Close basis {_fmt(prev_close)}, Expected High trước {_fmt(expected_high)}); điểm xác nhận SHORT {_fmt(trigger)}.",
            f"TP tại entry LONG t+1 {_fmt(long_entry)}; tối đa {max_size:.2f} vị thế; chưa TP/SL thì đóng ATC.",
        ]
        op = snapshot.session_open
        lo = snapshot.session_low
        if op is None:
            lines.append("Chưa có Open nên chưa biết phiên này có đủ điều kiện Open ≥ Upper; gửi O–H–L–P để kích hoạt đúng rule.")
        elif float(op) < upper:
            lines.append(f"Không kích hoạt: Open {_fmt(op)} thấp hơn Upper {_fmt(upper)}. Rule này không cho SHORT đuổi khi không có gap lên trên Upper.")
        elif price is None:
            lines.append(f"Open {_fmt(op)} đủ điều kiện; nhưng chưa có giá hiện tại. Chỉ SHORT sau khi giá rơi dưới {_fmt(trigger)}, không bán sớm trên trigger.")
        elif float(price) <= float(long_entry):
            lines.append(f"Giá {_fmt(price)} đã tới/xuyên TP {_fmt(long_entry)}: không mở SHORT mới; chuyển sang kiểm tra kèo LONG chính.")
        elif float(price) > trigger:
            if lo is not None and float(lo) <= trigger and float(price) >= upper:
                lines.append(f"Nhịp reclaim đã thất bại: Low từng rơi dưới {_fmt(trigger)} nhưng giá vượt lại Upper {_fmt(upper)}; đóng/không mở SHORT.")
            else:
                lines.append(f"Chưa SHORT: giá {_fmt(price)} còn trên điểm reclaim {_fmt(trigger)}. Không bán sớm trước xác nhận.")
        else:
            lines.append(f"Đã reclaim xuống: giá {_fmt(price)} ≤ {_fmt(trigger)} và còn trên TP {_fmt(long_entry)}; có thể SHORT scalp, SL {_fmt(upper)}, TP {_fmt(long_entry)}, size tối đa {max_size:.2f}.")
        metrics = audit.get("full", {})
        if metrics:
            lines.append(f"Audit 2018–2026: {int(metrics.get('trades',0))} lệnh, WR {float(metrics.get('wr_pct',0)):.2f}%, PnL sau phí {float(metrics.get('pnl_points',0)):+.2f}, MaxDD {float(metrics.get('max_dd_points',0)):.2f} điểm.")
        return self._limit_answer_lines(lines, max_lines=6)

    def _answer_side_plan(
        self,
        context: dict[str, Any],
        focus_plan: dict[str, Any] | None,
        question: str,
        snapshot: SessionSnapshot,
    ) -> list[str]:
        requested = self._detect_requested_side(question)
        primary = focus_plan or self._primary_plan(context)
        if not requested:
            return self._focused_current_plan(context)
        if not primary:
            return [f"Chưa có kèo frozen hợp lệ để đánh giá phương án {requested}; tạm đứng ngoài."]
        system_side = str(primary.get("direction") or "").upper()
        if requested != system_side:
            return self._answer_countertrend_plan(context, primary, question, snapshot)

        r5 = self._r5_control(context)
        r5_action = str(r5.get("current_action") or "NO_SIGNAL").upper()
        if r5_action == "CANCEL":
            return [
                f"R5 đang CANCEL/units 0: NO TRADE, không mở {requested} dù direction frozen vẫn là {system_side}.",
                "Chờ outputs mới; không dùng đường giá giả định sau cancel để tự khôi phục kèo.",
            ]
        if r5_action == "FLIP_HINT":
            return [
                f"R5 đang FLIP_HINT ngược hướng frozen {system_side}: chưa mở mới {requested} cùng hướng frozen.",
                "Chỉ đánh giá phía đảo sau khi Open/intraday xác nhận; nếu chưa đủ OHLC thì đứng ngoài.",
            ]

        entry = primary.get("operational_entry")
        target = primary.get("operational_target")
        price = snapshot.live_price
        lines = [f"{requested} là cùng hướng kèo chính của hệ: chờ {_fmt(entry)}, mục tiêu {_fmt(target)}."]
        if price is None:
            lines.append("Chưa có giá hiện tại nên chưa thể kết luận vào ngay hay tiếp tục chờ; cung cấp giá live, tốt hơn kèm Open–High–Low.")
            return lines

        if entry is None:
            lines.append("Kèo chưa có entry operational hợp lệ; không vào market chỉ vì đúng hướng.")
            return lines
        if requested == "SHORT":
            distance = float(entry) - float(price)
            if distance > 0:
                lines.append(f"Giá {_fmt(price)} còn thấp hơn vùng chờ {_fmt(distance)} điểm: không SHORT đuổi ở đây; đợi hồi lên entry và xác nhận ngừng tăng.")
            else:
                lines.append(f"Giá {_fmt(price)} đã chạm/vượt vùng chờ; chỉ SHORT khi trạng thái fill được xác nhận, không suy từ một con số đơn lẻ.")
        else:
            distance = float(price) - float(entry)
            if distance > 0:
                lines.append(f"Giá {_fmt(price)} còn cao hơn vùng chờ {_fmt(distance)} điểm: không LONG đuổi; đợi điều chỉnh về entry và xác nhận ngừng rơi.")
            else:
                lines.append(f"Giá {_fmt(price)} đã chạm/thấp hơn vùng chờ; chỉ LONG khi trạng thái fill được xác nhận.")
        lines.append(f"Sau khi khớp mới quản lý theo mục tiêu {_fmt(target)}; target bị đi qua trước fill không được tính là thắng.")
        return lines

    def _answer_countertrend_plan(
        self,
        context: dict[str, Any],
        focus_plan: dict[str, Any] | None,
        question: str,
        snapshot: SessionSnapshot,
    ) -> list[str]:
        requested = self._detect_requested_side(question)
        primary = focus_plan or self._primary_plan(context)
        if not requested:
            return ["Chưa xác định được khách muốn LONG hay SHORT; hãy nói rõ hướng muốn đánh."]
        if not primary:
            return ["Chưa có kèo frozen hợp lệ để làm mốc đối chiếu; chưa nên dựng giao dịch ngược hệ."]

        system_side = str(primary.get("direction") or "").upper()
        system_entry = primary.get("operational_entry")
        system_target = primary.get("operational_target")
        price = snapshot.live_price
        session_range = None
        if snapshot.session_high is not None and snapshot.session_low is not None:
            session_range = abs(snapshot.session_high - snapshot.session_low)
        opposite = system_side in {"LONG", "SHORT"} and requested != system_side

        if not opposite:
            return [
                f"Hướng khách muốn {requested} đang cùng hướng kèo hệ {system_side or 'chưa rõ'}.",
                "Đây không phải giao dịch ngược hệ; hãy dùng đúng entry, target và trạng thái fill của kèo chính.",
            ]

        if requested == "LONG" and system_side == "SHORT":
            try:
                below_waiting_short = system_entry is not None and (price is None or float(price) < float(system_entry))
            except (TypeError, ValueError):
                below_waiting_short = False
            if below_waiting_short:
                return self._answer_bridge_long_to_short(context, primary, snapshot)
        if requested == "SHORT" and system_side == "LONG":
            try:
                above_waiting_long = system_entry is not None and (price is None or float(price) > float(system_entry))
            except (TypeError, ValueError):
                above_waiting_long = False
            if above_waiting_long:
                return self._answer_bridge_short_to_long(context, primary, snapshot)

        allowed, r5_gate, r5_mode = self._r5_countertrend_gate(context, system_side, requested, snapshot)
        if not allowed:
            return r5_gate

        lines = list(r5_gate)
        lines.append(
            f"Có thể lập kế hoạch {requested} ngược nhịp, nhưng hệ vẫn xác nhận hướng chính là {system_side}; không được gọi đây là kèo hệ."
        )
        if system_entry is None:
            lines.append("Kèo hệ chưa có entry hợp lệ nên chưa có mốc khách quan để đặt điểm thoát giao dịch ngược nhịp.")
            return lines

        # The opposing system entry is a logical take-profit/decision zone, not proof that price will reach it.
        if requested == "LONG" and system_side == "SHORT":
            relation = "lên"
            room = (float(system_entry) - float(price)) if price is not None else None
            favorable = room is None or room > 0
        else:
            relation = "xuống"
            room = (float(price) - float(system_entry)) if price is not None else None
            favorable = room is None or room > 0

        lines.append(
            f"Vùng {_fmt(system_entry)} chỉ được dùng làm vùng chốt/đánh giá lại khi giá hồi {relation}; chạm vùng này phải đóng giao dịch ngược nhịp trước, không tự động đảo sang {system_side}."
        )
        if not r5_gate:
            lines.append(f"Nếu vẫn chọn đánh ngược hệ, chỉ dùng {self._r5_size_text(context)}; không bình quân khi giá đi ngược.")
        if price is None:
            lines.append("Để đánh giá có còn đáng vào hay không, hãy cung cấp giá hiện tại; tốt hơn thêm Open–High–Low của phiên.")
            lines.append("Thiếu giá live thì hệ không thể tính dư địa, mức đuổi giá và vị trí trong biên phiên một cách an toàn.")
            return lines

        if not favorable or room is None:
            lines.append(f"Giá hiện tại {_fmt(price)} đã đi qua hoặc nằm sai phía vùng {_fmt(system_entry)}; luận điểm ăn nhịp tới vùng chờ {system_side} không còn hợp lệ.")
            lines.append("Không mở mới theo kế hoạch này; cần dựng lại kịch bản từ dữ liệu OHLC mới.")
            return lines

        range_ratio = float(room) / float(session_range) if session_range not in (None, 0) else None
        if range_ratio is not None:
            if range_ratio < 0.15:
                quality = "dư địa quá mỏng"
            elif range_ratio < 0.35:
                quality = "dư địa hẹp"
            elif range_ratio <= 0.9:
                quality = "dư địa còn thực dụng"
            else:
                quality = "mục tiêu khá xa trong một nhịp"
            lines.append(f"Từ giá {_fmt(price)} tới vùng chờ còn {_fmt(room)} điểm, bằng khoảng {range_ratio:.2f} lần biên High–Low hiện tại: {quality}.")
        else:
            lines.append(f"Từ giá {_fmt(price)} tới vùng chờ còn {_fmt(room)} điểm; cần thêm High–Low phiên hoặc dữ liệu OHLC lịch sử để chuẩn hóa dư địa.")

        # Tận dụng toàn bộ mức operational đã tính trong các engine/horizon để dựng thang xử lý,
        # nhưng không sáng tác mức mới. Mốc cuối luôn là entry của kèo đối ứng.
        entry_levels: list[float] = []
        target_levels: list[float] = []
        for plan in context.get("active_plans", []):
            for key, bucket in (("operational_entry", entry_levels), ("operational_target", target_levels)):
                value = plan.get(key)
                if value is not None:
                    try:
                        bucket.append(float(value))
                    except (TypeError, ValueError):
                        pass
        # Mốc chốt của giao dịch ngược nhịp lấy từ các entry đối ứng; target của kèo chính
        # chỉ dùng làm mốc bất lợi/risk. Như vậy không trộn ý nghĩa hai loại mức giá.
        if requested == "LONG":
            favorable_levels = sorted({x for x in entry_levels if float(price) < x <= float(system_entry)})
            adverse_levels = sorted({x for x in target_levels if x < float(price)}, reverse=True)
        else:
            favorable_levels = sorted({x for x in entry_levels if float(system_entry) <= x < float(price)}, reverse=True)
            adverse_levels = sorted({x for x in target_levels if x > float(price)})
        if float(system_entry) not in favorable_levels:
            favorable_levels.append(float(system_entry))
            favorable_levels = sorted(set(favorable_levels), reverse=(requested == "SHORT"))
        if favorable_levels:
            selected = favorable_levels if len(favorable_levels) <= 3 else [favorable_levels[0], favorable_levels[1], favorable_levels[-1]]
            levels_text = " → ".join(_fmt(x) for x in selected)
            lines.append(f"Thang chốt chỉ lấy từ mức hệ đã tính: {levels_text}; chốt dần, không giữ toàn bộ tới mốc cuối.")
        if adverse_levels:
            risk = adverse_levels[:2]
            if len(risk) >= 2:
                lines.append(f"Quản trị theo mốc hệ có sẵn: mất {_fmt(risk[0])} thì giảm ngay, mất {_fmt(risk[1])} thì thoát; không bình quân ngược hướng chính.")
            else:
                lines.append(f"Mốc bất lợi gần nhất hệ có sẵn là {_fmt(risk[0])}; mất mốc này phải giảm rủi ro, không bình quân.")

        if snapshot.session_open is None or snapshot.session_high is None or snapshot.session_low is None:
            lines.append("Cần thêm Open–High–Low hiện tại để biết LONG/SHORT này đang mua đáy hồi hay đang đuổi cuối biên; chưa đủ OHLC thì chỉ nên coi là kịch bản, không phải khuyến nghị vào ngay.")
        else:
            span = float(snapshot.session_high) - float(snapshot.session_low)
            pos = ((float(price) - float(snapshot.session_low)) / span) if span > 0 else None
            if pos is not None:
                zone = "gần đáy biên" if pos <= 0.3 else ("giữa biên" if pos < 0.7 else "gần đỉnh biên")
                if requested == "LONG":
                    trigger = f"chỉ LONG khi giá ngừng tạo đáy thấp hơn và giữ/reclaim được Open {_fmt(snapshot.session_open)}"
                else:
                    trigger = f"chỉ SHORT khi giá ngừng tạo đỉnh cao hơn và mất lại Open {_fmt(snapshot.session_open)}"
                lines.append(f"Giá đang ở {pos:.0%} biên phiên ({zone}); {trigger}, không vào chỉ vì khách muốn ngược hệ.")

        lines.append(f"Điều làm hủy kế hoạch: kèo hệ chuyển CANCEL/units 0, entry {_fmt(system_entry)} đổi, hoặc OHLC mới cho thấy nhịp hồi thất bại; hệ không tự bịa mức ngoài dữ liệu đã tính.")
        return lines


    def _reasoning_frame(
        self,
        question: str,
        context: dict[str, Any],
        focus_plan: dict[str, Any] | None,
        memory: ConversationMemory,
        snapshot: SessionSnapshot,
    ) -> dict[str, Any]:
        """Build a reusable decision frame instead of answering from a keyword template."""
        primary = focus_plan or self._primary_plan(context)
        plans = context.get("active_plans", [])
        con = context.get("consensus", {})
        direction = str((primary or {}).get("direction") or con.get("direction") or "NA").upper()
        entry = (primary or {}).get("operational_entry")
        target = (primary or {}).get("operational_target")
        price = snapshot.live_price
        requested = self._detect_requested_side(question)
        qnorm = self._norm(question)
        followup_terms = ["nếu", "vậy", "thế", "tiếp", "thủng", "vượt", "lên", "xuống", "mất lại", "giữ được", "còn nếu"]
        if not requested and memory.tactical_side and any(term in qnorm for term in followup_terms):
            requested = memory.tactical_side
        relation = "unspecified"
        if requested and direction in {"LONG", "SHORT"}:
            relation = "aligned" if requested == direction else "countertrend"
        elif requested:
            relation = "requested_without_system_side"

        position = "unknown"
        distance_to_entry = None
        distance_to_target = None
        if price is not None and entry is not None:
            distance_to_entry = float(entry) - float(price)
            if direction == "SHORT":
                position = "below_entry" if price < entry else ("at_entry" if abs(price-entry)<1e-9 else "above_entry")
            elif direction == "LONG":
                position = "above_entry" if price > entry else ("at_entry" if abs(price-entry)<1e-9 else "below_entry")
        if price is not None and target is not None:
            distance_to_target = float(target) - float(price)

        fill_state = "unknown"
        if price is not None and entry is not None and direction in {"LONG", "SHORT"}:
            if direction == "SHORT":
                fill_state = "not_filled" if price < entry else "entry_reached"
            else:
                fill_state = "not_filled" if price > entry else "entry_reached"

        invalidation = []
        if primary:
            if str(primary.get("risk_action") or "").upper() == "CANCEL" or float(primary.get("units") or 1) == 0:
                invalidation.append("Kèo đã bị cancel hoặc units bằng 0")
            if direction == "SHORT" and entry is not None:
                invalidation.append(f"Giá giữ vững phía trên vùng chờ {_fmt(entry)} làm luận điểm SHORT yếu đi")
            elif direction == "LONG" and entry is not None:
                invalidation.append(f"Giá giữ vững phía dưới vùng chờ {_fmt(entry)} làm luận điểm LONG yếu đi")
        return {
            "primary": primary,
            "plans": plans,
            "system_side": direction,
            "requested_side": requested,
            "relation": relation,
            "price": price,
            "entry": entry,
            "target": target,
            "position": position,
            "fill_state": fill_state,
            "distance_to_entry": distance_to_entry,
            "distance_to_target": distance_to_target,
            "consensus": con,
            "warnings": context.get("warnings", []),
            "invalidation": invalidation,
            "missing": [name for name, value in (("giá hiện tại", price), ("Open", snapshot.session_open), ("High", snapshot.session_high), ("Low", snapshot.session_low)) if value is None],
        }

    def _next_output_level(self, context: dict[str, Any], *, above: float | None = None, below: float | None = None) -> float | None:
        levels: list[float] = []
        for plan in context.get("active_plans", []):
            for key in ("operational_entry", "operational_target", "forecast", "entry", "exit"):
                value = plan.get(key)
                try:
                    if value is not None:
                        levels.append(float(value))
                except (TypeError, ValueError):
                    pass
        levels = sorted(set(round(x, 6) for x in levels))
        if above is not None:
            candidates = [x for x in levels if x > float(above) + 1e-9]
            return candidates[0] if candidates else None
        if below is not None:
            candidates = [x for x in levels if x < float(below) - 1e-9]
            return candidates[-1] if candidates else None
        return None

    def _accountability_recovery_lines(
        self,
        question: str,
        context: dict[str, Any],
        plan: dict[str, Any],
        memory: ConversationMemory,
        snapshot: SessionSnapshot,
        frame: dict[str, Any],
    ) -> list[str]:
        side = str(plan.get("direction") or "").upper()
        entry = float(plan.get("operational_entry")) if plan.get("operational_entry") is not None else None
        target = float(plan.get("operational_target")) if plan.get("operational_target") is not None else None
        price = float(snapshot.live_price) if snapshot.live_price is not None else None
        high = float(snapshot.session_high) if snapshot.session_high is not None else None
        low = float(snapshot.session_low) if snapshot.session_low is not None else None
        claimed_side, claimed_entry = self._extract_position_claim(question)
        held_side = claimed_side or (memory.tactical_side if memory.position_status == "OPEN" else "")
        held_entry = claimed_entry if claimed_entry is not None else memory.tactical_entry
        r5 = self._r5_control(context)
        action = str(r5.get("current_action") or "NO_SIGNAL").upper()
        cap_text = self._r5_size_text(context)
        q = self._norm(question)
        lines: list[str] = []
        responsibility = any(x in q for x in ["chịu trách nhiệm", "chiu trach nhiem", "tại mày", "tai may", "mày sai", "may sai"])
        exited = any(x in q for x in ["cắt rồi", "cat roi", "vừa cắt", "vua cat", "đóng rồi", "dong roi"])
        asks_recover_loss = any(x in q for x in ["gỡ", "go lo", "gỡ lỗ", "go lo"])

        if price is None:
            lines.append("Tao không chống chế kèo cũ khi chưa biết giá hiện tại: cần giá live, và tốt nhất thêm Open–High–Low, để xác định kèo chỉ đang drawdown hay đã bị vô hiệu.")
            if held_side:
                lines.append(f"Tao đã ghi nhận khách đang {held_side}" + (f" từ {_fmt(held_entry)}" if held_entry is not None else "") + "; chưa được bình quân cho tới khi có giá live.")
            return lines

        tol = max(1.5, ((high - low) * 0.10) if high is not None and low is not None and high > low else 2.0)
        short_invalid = side == "SHORT" and entry is not None and price > entry + tol
        long_invalid = side == "LONG" and entry is not None and price < entry - tol
        invalidated = short_invalid or long_invalid

        if responsibility:
            lines.append("Tao chịu trách nhiệm ở mức tư vấn: thừa nhận ngay khi luận điểm cũ bị vô hiệu, ưu tiên cắt rủi ro và không bịa một lệnh mới để che sai.")
        if exited:
            frame["tactical_side"], frame["tactical_mode"] = "FLAT", f"RECOVERY_AFTER_{side}_INVALIDATION"
            if asks_recover_loss:
                lines.append("Đã cắt thì không có 'kèo gỡ' ngay. Chỉ nhận lệnh mới khi một nhánh R5 được xác nhận; bỏ lỡ còn tốt hơn revenge trade.")

        if invalidated:
            adverse = "trên" if side == "SHORT" else "dưới"
            lines.append(
                f"Đúng: với giá {_fmt(price)} đang giữ {adverse} entry {side} {_fmt(entry)}, kèo vào tại {_fmt(entry)} đã bị vô hiệu ở thời điểm này. Tao không bảo vệ dự báo cũ và không gọi đây là drawdown bình thường."
            )
            if held_side == side:
                loss = None
                if held_entry is not None:
                    loss = (float(held_entry) - price) if held_side == "LONG" else (price - float(held_entry))
                loss_text = f" (bất lợi khoảng {_fmt(abs(loss))} điểm)" if loss is not None else ""
                lines.append(f"Nếu khách đang {held_side}{(' từ ' + _fmt(held_entry)) if held_entry is not None else ''}{loss_text}: đóng/giảm về 0 theo thanh khoản; không bình quân và không chờ giá quay về hòa vốn.")
            else:
                lines.append(f"Không mở mới {side} và không revenge trade quanh {_fmt(entry)}; vị thế cũ được coi là đã kết thúc.")
            frame["tactical_side"] = "FLAT"
            frame["tactical_mode"] = f"RECOVERY_AFTER_{side}_INVALIDATION"
        else:
            lines.append(f"Kèo {side} chưa đủ dữ kiện để kết luận sai hoàn toàn, nhưng phải quản trị theo giá hiện tại {_fmt(price)} chứ không bám câu dự báo ban đầu.")

        next_above = self._next_output_level(context, above=entry if entry is not None else price)
        next_below = self._next_output_level(context, below=entry if entry is not None else price)

        if action == "CANCEL":
            lines.append("R5 đang CANCEL/units 0: sau khi xử lý vị thế cũ thì NO TRADE, không tìm lệnh mới để gỡ và không tự đảo chiều.")
            return lines

        if action == "FLIP_HINT":
            opposite = "LONG" if side == "SHORT" else "SHORT"
            if side == "SHORT":
                objective = next_above
                lines.append(f"R5 đã FLIP_HINT: chỉ xét {opposite} sau retest {_fmt(entry)} giữ được, với {cap_text}; không {opposite} đuổi tại {_fmt(price)}." + (f" Mốc chốt gần từ outputs: {_fmt(objective)}." if objective is not None else ""))
            else:
                objective = next_below
                lines.append(f"R5 đã FLIP_HINT: chỉ xét {opposite} sau retest {_fmt(entry)} thất bại/giữ dưới, với {cap_text}; không {opposite} đuổi tại {_fmt(price)}." + (f" Mốc chốt gần từ outputs: {_fmt(objective)}." if objective is not None else ""))
            return lines

        if action == "KEEP":
            if side == "SHORT":
                lines.append(f"R5 vẫn KEEP SHORT nên không tự đảo LONG. Kèo mới chỉ trở lại khi giá mất lại {_fmt(entry)}, retest không vượt được và fill/rejection đúng thứ tự; khi đó mục tiêu {_fmt(target)}.")
            else:
                lines.append(f"R5 vẫn KEEP LONG nên không tự đảo SHORT. Kèo mới chỉ trở lại khi giá lấy lại {_fmt(entry)}, retest giữ được và fill/reclaim đúng thứ tự; khi đó mục tiêu {_fmt(target)}.")
            return lines

        # PRE_OPEN/PENDING: give a two-branch map, but never present it as an active order.
        if side == "SHORT":
            extra = f", hướng tới {_fmt(next_above)}" if next_above is not None else ""
            lines.append(f"R5 hiện mới PRE_OPEN: không LONG đuổi. Nhánh breakout chỉ được kích hoạt nếu giá retest {_fmt(entry)} rồi giữ trên và base cập nhật FLIP_HINT/plan mới; khi đó LONG thăm dò {cap_text}{extra}.")
            lines.append(f"Nhánh quay lại hệ: nếu giá mất {_fmt(entry)}, retest không lấy lại được và outputs sau OPEN vẫn KEEP SHORT thì mới SHORT lại {_fmt(entry)} → {_fmt(target)}; không vào chỉ để gỡ lỗ.")
        else:
            extra = f", hướng tới {_fmt(next_below)}" if next_below is not None else ""
            lines.append(f"R5 hiện mới PRE_OPEN: không SHORT đuổi. Nhánh breakdown chỉ được kích hoạt nếu giá retest {_fmt(entry)} rồi giữ dưới và base cập nhật FLIP_HINT/plan mới; khi đó SHORT thăm dò {cap_text}{extra}.")
            lines.append(f"Nhánh quay lại hệ: nếu giá lấy lại {_fmt(entry)}, retest giữ được và outputs sau OPEN vẫn KEEP LONG thì mới LONG lại {_fmt(entry)} → {_fmt(target)}; không vào chỉ để gỡ lỗ.")
        return lines

    def _symbolic_tactical_followup_lines(
        self,
        question: str,
        context: dict[str, Any],
        plan: dict[str, Any],
        memory: ConversationMemory,
        snapshot: SessionSnapshot,
    ) -> list[str]:
        """Manage a previously accepted tactical position from natural, non-numeric follow-ups.

        Examples: "mất lại Open", "vượt High và giữ", "thủng Low".  These are
        conditional instructions, not fabricated live prices, so they must not be fed
        through the generic price parser as if a new tick had been supplied.
        """
        tactical = str(memory.tactical_side or "").upper()
        q = self._norm(question)
        q_ascii = "".join(ch for ch in unicodedata.normalize("NFD", q) if unicodedata.category(ch) != "Mn")
        recovery_mode = str(memory.tactical_mode or "").upper()
        entry = plan.get("operational_entry")
        mentioned = self._parse_numbers(question)
        mentions_entry = bool(entry is not None and any(abs(float(x) - float(entry)) <= 1.5 for x in mentioned))
        if recovery_mode.startswith("RECOVERY_AFTER_"):
            system_side = str(plan.get("direction") or "").upper()
            action = str(self._r5_control(context).get("current_action") or "NO_SIGNAL").upper()
            requested = self._detect_requested_side(question)
            live = snapshot.live_price
            next_above = self._next_output_level(context, above=float(entry)) if entry is not None else None
            next_below = self._next_output_level(context, below=float(entry)) if entry is not None else None
            false_break_reclaim = mentions_entry and any(x in q_ascii for x in ["thung", "mat", "pha"]) and any(x in q_ascii for x in ["bat lai", "lay lai", "vuot lai", "reclaim"])
            if false_break_reclaim:
                if system_side == "SHORT":
                    return [
                        f"Thủng {_fmt(entry)} rồi bật lấy lại phía trên là false breakdown: không SHORT lại; nhịp mất entry chưa được xác nhận.",
                        f"R5 hiện {action}. Chỉ khi retest {_fmt(entry)} giữ trên và R5 phát FLIP_HINT mới xét LONG nhỏ; còn mất lại rồi retest thất bại mới quay về SHORT.",
                    ]
                return [
                    f"Vượt {_fmt(entry)} rồi rơi giữ lại phía dưới là false breakout: không LONG lại; nhịp reclaim chưa được xác nhận.",
                    f"R5 hiện {action}. Chỉ khi retest {_fmt(entry)} giữ dưới và R5 phát FLIP_HINT mới xét SHORT nhỏ; còn lấy lại rồi retest thành công mới quay về LONG.",
                ]
            if any(x in q_ascii for x in ["lo no chay", "lo chay", "chay tiep", "so lo", "fomo"]):
                level = float(mentioned[-1]) if mentioned else (next_above if system_side == "SHORT" else next_below)
                return [
                    f"Không vào vì sợ lỡ. Nếu giá chạy tiếp tới {_fmt(level)}, bỏ lỡ là kết quả chấp nhận được; vào đuổi sau một kèo sai chỉ làm R:R xấu hơn.",
                    f"Nếu đã có vị thế từ retest hợp lệ thì chốt bớt ở {_fmt(level)}; nếu chưa có thì chờ retest {_fmt(entry)} và quyền R5 mới.",
                ]
            if requested:
                if action == "CANCEL":
                    return [
                        "R5 đang CANCEL/units 0: sau khi đóng kèo sai thì NO TRADE, không mở lệnh mới để gỡ và không tự đảo chiều.",
                        "Chờ outputs phát action mới; evidence cũ và cảm xúc của khách không được ghi đè CANCEL.",
                    ]
                if requested != system_side:
                    objective = next_above if requested == "LONG" else next_below
                    if action == "KEEP":
                        return [
                            f"Không {requested} đuổi tại {_fmt(live)}. R5 đang KEEP {system_side}, nên chưa có quyền tự đảo sang {requested}; trước hết chờ retest {_fmt(entry)} và action mới.",
                            f"Nếu R5 sau đó phát FLIP_HINT thì mới xét {requested} với {self._r5_size_text(context)}" + (f", mốc gần {_fmt(objective)}." if objective is not None else "."),
                        ]
                    if action == "FLIP_HINT":
                        return [
                            f"R5 đã FLIP_HINT sang {requested}, nhưng không {requested} đuổi tại {_fmt(live)}. Kèo tốt hơn là chờ retest {_fmt(entry)} giữ đúng phía rồi mới vào.",
                            f"Khởi đầu theo khung {self._r5_size_text(context)}" + (f", chốt gần {_fmt(objective)}." if objective is not None else "."),
                        ]
                    return [
                        f"Không {requested} đuổi tại {_fmt(live)} sau khi kèo {system_side} vừa bị vô hiệu. Kèo tốt hơn là chờ retest {_fmt(entry)} xác nhận rồi mới xét phía {requested}.",
                        f"R5 hiện {action}: chỉ FLIP_HINT/plan mới cho phép kích hoạt {requested} với {self._r5_size_text(context)}" + (f", mốc gần {_fmt(objective)}." if objective is not None else "."),
                    ]
                if action == "FLIP_HINT":
                    return [
                        f"Không quay lại {system_side}: R5 đang FLIP_HINT sang phía đối diện và kèo {system_side} cũ vừa bị vô hiệu.",
                        "Chờ kịch bản flip được retest xác nhận; không vào chỉ để gỡ lỗ.",
                    ]
                return [
                    f"Không vào lại {system_side} chỉ để gỡ lỗ. Phải chờ giá quay lại {_fmt(entry)}, retest xác nhận và R5/outputs sau OPEN còn cho hướng {system_side}.",
                    f"Nếu điều kiện đó đạt, mục tiêu hệ là {_fmt(plan.get('operational_target'))}; còn nếu giá tiếp tục giữ phía bất lợi thì đứng ngoài.",
                ]
            if any(x in q_ascii for x in ["gio lam gi", "bay gio lam gi", "xu ly tiep", "keo moi", "co keo nao"]):
                return [
                    f"Sau khi đóng kèo sai, không gỡ ngay. Chờ một trong hai xác nhận quanh {_fmt(entry)}: giữ phía breakout + FLIP_HINT để đi ngược, hoặc mất lại vùng vào + KEEP để quay về hướng hệ.",
                    "Không có xác nhận thì đứng ngoài; mục tiêu là bảo toàn vốn, không phải buộc phải có lệnh để chứng minh dự báo đúng.",
                ]
            if mentions_entry and any(x in q_ascii for x in ["roi lai", "mat lai", "xuong lai", "ve lai", "cham lai"]):
                if system_side == "SHORT":
                    return [
                        f"Giá quay lại {_fmt(entry)} chưa tự động là kèo SHORT. Nếu xuyên xuống rồi retest không lấy lại được và R5/outputs sau OPEN vẫn cho SHORT thì mới vào lại → {_fmt(plan.get('operational_target'))}.",
                        f"Nếu chỉ retest {_fmt(entry)} rồi bật giữ trên, đó là breakout còn hiệu lực: không SHORT; chỉ xét LONG khi R5 phát FLIP_HINT, không đuổi giá.",
                    ]
                return [
                    f"Giá quay lại {_fmt(entry)} chưa tự động là kèo LONG. Nếu lấy lại rồi retest giữ được và R5/outputs sau OPEN vẫn cho LONG thì mới vào lại → {_fmt(plan.get('operational_target'))}.",
                    f"Nếu chỉ retest {_fmt(entry)} rồi bị ép giữ dưới, breakdown còn hiệu lực: không LONG; chỉ xét SHORT khi R5 phát FLIP_HINT.",
                ]
            if mentioned and any(x in q_ascii for x in ["vuot", "pha", "giu tren", "thung", "giu duoi"]):
                level = float(mentioned[-1])
                if system_side == "SHORT" and any(x in q_ascii for x in ["vuot", "pha", "giu tren"]):
                    return [
                        f"Nếu vượt và giữ trên {_fmt(level)}: không SHORT và không LONG đuổi. Nếu đã LONG từ retest {_fmt(entry)} hợp lệ thì chốt bớt tại {_fmt(level)}; nếu chưa có vị thế thì chờ retest mới.",
                        f"R5 hiện {action}; chỉ FLIP_HINT/plan mới cho phép duy trì phía LONG, còn CANCEL là NO TRADE.",
                    ]
                if system_side == "LONG" and any(x in q_ascii for x in ["thung", "pha", "giu duoi"]):
                    return [
                        f"Nếu thủng và giữ dưới {_fmt(level)}: không LONG và không SHORT đuổi. Nếu đã SHORT từ retest {_fmt(entry)} hợp lệ thì chốt bớt tại {_fmt(level)}; nếu chưa có vị thế thì chờ retest mới.",
                        f"R5 hiện {action}; chỉ FLIP_HINT/plan mới cho phép duy trì phía SHORT, còn CANCEL là NO TRADE.",
                    ]
        if tactical not in {"LONG", "SHORT"}:
            if mentions_entry and any(x in q_ascii for x in ["mat lai", "roi lai", "xuong lai", "ve lai"]):
                system_side = str(plan.get("direction") or "").upper()
                if system_side == "SHORT":
                    return [
                        f"Nếu giá hiện lùi xuống {_fmt(entry)}, có thể canh SHORT theo kế hoạch đã hiệu chỉnh sau khi retest không lấy lại được entry; không bán ngay chỉ vì vừa chạm lại.",
                        f"Mục tiêu {_fmt(plan.get('operational_target'))}; nếu giá bật giữ trên {_fmt(entry)} thì hủy ý tưởng SHORT, không bình quân.",
                    ]
                if system_side == "LONG":
                    return [
                        f"Nếu giá hiện hồi lên {_fmt(entry)}, có thể canh LONG theo kế hoạch đã hiệu chỉnh sau khi retest giữ được entry; không mua ngay chỉ vì vừa chạm lại.",
                        f"Mục tiêu {_fmt(plan.get('operational_target'))}; nếu giá rơi giữ dưới {_fmt(entry)} thì hủy ý tưởng LONG, không bình quân.",
                    ]
            return []
        open_p = snapshot.session_open
        high = snapshot.session_high
        low = snapshot.session_low
        system_side = str(plan.get("direction") or "").upper()
        r5 = self._r5_control(context)
        action = str(r5.get("current_action") or "NO_SIGNAL").upper()

        # R5 is the top-level permission layer.  A newly observed CANCEL invalidates
        # both the frozen trade and any bridge/flip scenario being discussed.
        if action == "CANCEL":
            return [
                "R5 hiện là CANCEL/units 0: đóng hoặc hủy mọi ý tưởng đang quản lý, không mở lệnh mới và không tự đảo chiều.",
                "Evidence lịch sử hay kịch bản trước đó không được ghi đè action CANCEL; chờ outputs phát action mới.",
            ]

        loses_open = any(x in q_ascii for x in ["mat lai open", "mat open", "duoi open", "thung open", "roi lai open"])
        regains_open = any(x in q_ascii for x in ["vuot lai open", "tren open", "lay lai open", "reclaim open", "giu tren open"])
        breaks_low = any(x in q_ascii for x in ["thung low", "mat low", "duoi low", "pha low"])
        breaks_high = any(x in q_ascii for x in ["vuot high", "pha high", "tren high", "giu tren high"])
        holds_high = breaks_high and any(x in q_ascii for x in ["giu", "giu duoc", "giu tren"])
        holds_low = breaks_low and any(x in q_ascii for x in ["giu", "giu duoc", "giu duoi"])
        holds_above_entry = ("giu tren entry" in q_ascii) or (mentions_entry and "giu tren" in q_ascii)
        holds_below_entry = ("giu duoi entry" in q_ascii) or (mentions_entry and "giu duoi" in q_ascii)
        conditional_level = float(mentioned[-1]) if mentioned else None
        asks_up = conditional_level is not None and any(x in q_ascii for x in ["neu len", "hoi len", "len ", "vuot ", "tang len"])
        asks_down = conditional_level is not None and any(x in q_ascii for x in ["neu xuong", "roi xuong", "xuong ", "thung ", "mat "])

        if tactical == "LONG" and conditional_level is not None:
            if asks_up:
                if entry is not None and conditional_level >= float(entry) - 1.5:
                    return [
                        f"LONG ngược nhịp đã tới vùng thoát {_fmt(entry)}: chốt hết, không biến bridge thành kèo giữ dài.",
                        f"Chỉ cân nhắc SHORT sau khi giá từ chối vùng này, mất lại vùng vào {_fmt(entry)} và R5/outputs cho phép; không đảo chỉ vì vừa chốt LONG.",
                    ]
                levels = sorted({float(x) for x in [plan.get("operational_target"), plan.get("original_entry"), plan.get("original_target")] if x is not None})
                checkpoints = [x for x in levels if x <= conditional_level + 1.5]
                checkpoint = max(checkpoints) if checkpoints else conditional_level
                return [
                    f"Nếu đã có LONG scalp thì chốt bớt quanh mốc {_fmt(checkpoint)} khi giá lên {_fmt(conditional_level)}; chỉ giữ phần nhỏ nếu vượt và giữ được vùng này.",
                    f"Phần còn lại vẫn phải đóng trước/ở entry hệ {_fmt(entry)}; mất Open {_fmt(open_p)} thì giảm, không bình quân.",
                ]
            if asks_down:
                return [
                    f"Nếu đang LONG mà giá thủng {_fmt(conditional_level)}: thoát hết, không bình quân; kịch bản hồi đã sai.",
                    f"Chưa LONG thì không bắt đáy: phải chờ reclaim Open {_fmt(open_p)}; coi Low mới dưới {_fmt(conditional_level)} rồi mới đánh giá lại.",
                ]

        if tactical == "SHORT" and conditional_level is not None:
            if asks_down:
                if entry is not None and conditional_level <= float(plan.get("operational_target") or conditional_level) + 1.5:
                    return [
                        f"SHORT đã tới vùng mục tiêu {_fmt(plan.get('operational_target'))}: chốt phần lớn hoặc chốt hết theo kế hoạch, không tham giữ quá target.",
                        "Không tự đảo LONG sau khi chốt; chỉ xét lệnh mới khi R5/outputs phát kịch bản mới.",
                    ]
                return [
                    f"Nếu đã có SHORT thì chốt bớt khi giá xuống {_fmt(conditional_level)}; giữ phần nhỏ chỉ khi breakdown được giữ vững.",
                    f"Mục tiêu hệ còn lại là {_fmt(plan.get('operational_target'))}; vượt lại Open {_fmt(open_p)} thì giảm.",
                ]
            if asks_up:
                return [
                    f"Giá đã vượt entry SHORT {_fmt(entry)} và giữ trên {_fmt(conditional_level)}: đóng SHORT, không bình quân bán lên; nhịp từ chối đã thất bại.",
                    "Không tự đảo LONG sau khi thoát; chỉ xét phía đối diện khi R5 phát FLIP_HINT hoặc outputs mới xác nhận.",
                ]

        if tactical == "LONG":
            if holds_below_entry:
                return [
                    f"Nếu LONG mà giá giữ dưới vùng vào {_fmt(entry)}: đóng LONG; luận điểm reclaim/bridge đã thất bại, không bình quân.",
                    f"Không tự đảo SHORT chỉ vì vừa thoát. Chỉ xét SHORT khi R5 chuyển quyền phù hợp và có rejection/fill đúng thứ tự tại vùng {_fmt(entry)}.",
                ]
            if breaks_low:
                return [
                    f"Nếu thủng và giữ dưới Low{(' ' + _fmt(low)) if low is not None else ''}: thoát hết LONG; kịch bản hồi đã sai, không bình quân.",
                    f"Sau khi thoát không tự đảo SHORT; chỉ xét SHORT nếu R5/action và vùng entry frozen { _fmt(plan.get('operational_entry')) } cùng xác nhận.",
                ]
            if loses_open:
                return [
                    f"Nếu mất lại Open{(' ' + _fmt(open_p)) if open_p is not None else ''}: giảm mạnh hoặc đóng LONG scalp, vì điều kiện reclaim đã hỏng.",
                    f"Nếu sau đó còn thủng Low{(' ' + _fmt(low)) if low is not None else ''} thì thoát hết; không bình quân. R5 vẫn đang {action}, nên không tự biến việc thoát LONG thành lệnh SHORT.",
                ]
            if holds_high:
                levels = self._levels_between(context, float(high if high is not None else snapshot.live_price or 0), float(plan.get("operational_entry") or high or snapshot.live_price or 0)) if (high is not None or snapshot.live_price is not None) else []
                nxt = [x for x in levels if high is None or x > float(high) + 1e-9]
                target_text = f"; mốc kế tiếp từ outputs là {_fmt(nxt[0])}" if nxt else ""
                return [
                    f"Nếu vượt và giữ trên High{(' ' + _fmt(high)) if high is not None else ''}: giữ phần LONG còn lại nhỏ, dời quản trị lên vùng breakout{target_text}.",
                    f"Giữ tổng vị thế trong khung R5 ({self._r5_size_text(context)}); vẫn phải chốt bridge trước/vào vùng entry frozen {_fmt(plan.get('operational_entry'))} nếu chưa có FLIP_HINT mới.",
                ]
        else:  # tactical SHORT
            if holds_above_entry:
                return [
                    f"Nếu đã SHORT mà giá giữ vững trên vùng vào {_fmt(entry)}: đóng SHORT; nhịp từ chối không còn hiệu lực, không bình quân.",
                    f"Không tự đảo LONG sau khi thoát. Chỉ mở phía đối diện nếu R5 phát FLIP_HINT hoặc outputs mới xác nhận một kịch bản khác.",
                ]
            if breaks_high:
                return [
                    f"Nếu vượt và giữ trên High{(' ' + _fmt(high)) if high is not None else ''}: thoát hết SHORT; nhịp từ chối đã thất bại, không bình quân.",
                    f"Sau khi thoát không tự đảo LONG; chỉ xét LONG nếu R5/action và kịch bản live cho phép.",
                ]
            if regains_open:
                return [
                    f"Nếu vượt lại và giữ trên Open{(' ' + _fmt(open_p)) if open_p is not None else ''}: giảm mạnh hoặc đóng SHORT scalp, vì điều kiện mất Open đã hỏng.",
                    f"Nếu sau đó còn vượt High{(' ' + _fmt(high)) if high is not None else ''} thì thoát hết; R5 đang {action}, không tự đảo LONG.",
                ]
            if holds_low:
                levels = self._levels_between(context, float(plan.get("operational_entry") or low or snapshot.live_price or 0), float(low if low is not None else snapshot.live_price or 0)) if (low is not None or snapshot.live_price is not None) else []
                nxt = [x for x in levels if low is None or x < float(low) - 1e-9]
                target_text = f"; mốc kế tiếp từ outputs là {_fmt(nxt[-1])}" if nxt else ""
                return [
                    f"Nếu thủng và giữ dưới Low{(' ' + _fmt(low)) if low is not None else ''}: giữ phần SHORT còn lại nhỏ và kéo quản trị xuống theo breakdown{target_text}.",
                    f"Giữ tổng vị thế trong khung R5 ({self._r5_size_text(context)}); chốt dần theo các mức outputs, không biến scalp thành vị thế vô thời hạn.",
                ]
        return []

    def _session_reasoning_answer(
        self,
        question: str,
        intent: str,
        context: dict[str, Any],
        focus_plan: dict[str, Any] | None,
        memory: ConversationMemory,
        snapshot: SessionSnapshot,
    ) -> tuple[list[str], dict[str, Any]]:
        f = self._reasoning_frame(question, context, focus_plan, memory, snapshot)
        p = f["primary"]
        if not p:
            return ["Outputs chưa có kèo active hợp lệ để lập kế hoạch phiên."], f
        side, requested = f["system_side"], f["requested_side"]
        entry, target, price = f["entry"], f["target"], f["price"]
        unanimous = bool(f["consensus"].get("is_unanimous"))
        lines: list[str] = []
        warning_lines = self._dominant_warning_lines(context, p, snapshot, evidence=True, action=True)
        mention_v44 = "v44" in self._fold(question)
        if p.get("entry_target_swap_applied"):
            warning_lines = [self._plain_original_pair_explanation(p, mention_internal_label=mention_v44)]
        warning_seen = any(("Cảnh báo cụ thể:" in turn.answer or "hai mức giá gốc" in turn.answer) for turn in memory.turns[-6:])

        def mark_decision(action_lines: list[str]) -> None:
            joined = " ".join(action_lines)
            rejected = any(x in joined for x in [
                "không mở", "chưa mở", "không được gọi", "NO TRADE", "đã tới vùng thoát",
                "không LONG", "không SHORT", "không tự đảo", "chỉ được dựng kịch bản",
            ])
            if rejected:
                return
            if "có thể LONG scalp ngược nhịp" in joined or "được phép đánh giá LONG ngược" in joined:
                f["tactical_side"], f["tactical_mode"] = "LONG", "countertrend"
            elif "có thể SHORT scalp ngược nhịp" in joined or "được phép đánh giá SHORT ngược" in joined:
                f["tactical_side"], f["tactical_mode"] = "SHORT", "countertrend"
            elif "có thể canh SHORT theo kế hoạch" in joined or "OHLC đã đáp ứng điều kiện SHORT" in joined or "OHLC đã thỏa điều kiện kích hoạt SHORT" in joined:
                f["tactical_side"], f["tactical_mode"] = "SHORT", "aligned"
            elif "có thể canh LONG theo kế hoạch" in joined or "OHLC đã đáp ứng điều kiện LONG" in joined or "OHLC đã thỏa điều kiện kích hoạt LONG" in joined:
                f["tactical_side"], f["tactical_mode"] = "LONG", "aligned"

        if intent == "ACCOUNTABILITY_RECOVERY":
            recovery_lines = self._accountability_recovery_lines(question, context, p, memory, snapshot, f)
            return self._limit_answer_lines(recovery_lines, max_lines=7), f

        if intent == "R5_GUIDANCE":
            r5_lines = self._answer_r5_guidance(context, p, question, snapshot)
            return self._limit_answer_lines(r5_lines, max_lines=7), f

        symbolic_lines = self._symbolic_tactical_followup_lines(question, context, p, memory, snapshot)
        if symbolic_lines:
            # This manages an already accepted tactical position; it must not invent
            # a new tick or overwrite the remembered side merely because a condition
            # was discussed.
            return self._limit_answer_lines(symbolic_lines, max_lines=7), f

        # Explicit side: action first when complete OHLC is available.
        if f["relation"] in {"countertrend", "aligned"}:
            action_lines = self._intraday_action_lines(context, p, snapshot, requested_side=f["requested_side"])
            if action_lines:
                mark_decision(action_lines)
                lines.extend(action_lines)
                if warning_lines and not warning_seen:
                    lines.append(warning_lines[0])
                    if str((context.get("r5_control") or {}).get("current_action") or "").upper() == "CANCEL":
                        lines.append("R5 CANCEL hiện tại có quyền ưu tiên cao hơn evidence lịch sử: hôm nay vẫn NO TRADE cho tới khi outputs phát action mới.")
                    else:
                        if snapshot.is_completed_bar:
                            pass  # Action lines already state exact NO FILL/NO TRADE result.
                        else:
                            summary = self._warning_action_summary(self._dominant_warning(context, p), p)
                            if summary:
                                lines.append(summary)
                            if str((context.get("r5_control") or {}).get("current_action") or "").upper() == "PRE_OPEN":
                                lines.append("R5 hiện mới PRE_OPEN: đây là bản đồ xử lý theo OHLC, chưa phải lệnh chính thức; chờ base cập nhật KEEP/CANCEL/FLIP_HINT.")
            else:
                side_lines = self._answer_side_plan(context, p, question, snapshot)
                lines.extend(side_lines)
                if warning_lines and not warning_seen:
                    lines.append(warning_lines[0])
                    if str((context.get("r5_control") or {}).get("current_action") or "").upper() == "CANCEL":
                        lines.append("R5 CANCEL hiện tại có quyền ưu tiên cao hơn evidence lịch sử: hôm nay vẫn NO TRADE cho tới khi outputs phát action mới.")
                    else:
                        if snapshot.is_completed_bar:
                            pass  # Action lines already state exact NO FILL/NO TRADE result.
                        else:
                            summary = self._warning_action_summary(self._dominant_warning(context, p), p)
                            if summary:
                                lines.append(summary)
                            if str((context.get("r5_control") or {}).get("current_action") or "").upper() == "PRE_OPEN":
                                lines.append("R5 hiện mới PRE_OPEN: đây là bản đồ xử lý theo OHLC, chưa phải lệnh chính thức; chờ base cập nhật KEEP/CANCEL/FLIP_HINT.")
            return self._limit_answer_lines(lines, max_lines=7), f

        if intent == "FORECAST_CHART":
            chart_lines = self._focused_forecast_chart(context)
            return self._limit_answer_lines((warning_lines[:1] + chart_lines) if warning_lines else chart_lines, max_lines=5), f
        if intent == "FORECAST_RANGE":
            range_lines = self._focused_forecast_range(context, snapshot, question, p)
            return self._limit_answer_lines(range_lines, max_lines=5), f

        if price is None:
            lines.append(f"Chưa thể nói vào ngay vì chưa có giá hiện tại. Kế hoạch đang được phép dùng là {side} {_fmt(entry)} → {_fmt(target)}" + ("; các hệ cùng hướng nhưng vẫn phải đủ điều kiện giá." if unanimous else "."))
            if warning_lines:
                lines.append(warning_lines[0])
                if intent in {"QUALITY", "WHY"}:
                    profile = self._warning_evidence_profile(self._dominant_warning(context, p))
                    if profile.get("raw_negative_all") and profile.get("swap_positive_all"):
                        lines.append("Dữ liệu 2018–2026: cặp giá gốc thua ở cả giai đoạn xây dựng và hai giai đoạn kiểm tra độc lập; cặp đã sửa dương ở cả ba.")
                summary = self._warning_action_summary(self._dominant_warning(context, p), p)
                if summary:
                    lines.append(summary)
            opposite = "LONG" if side == "SHORT" else "SHORT"
            lines.append(f"Cách xử lý: giá về đúng vùng vào thì theo {side} đã sửa. Kèo ngược chiều chỉ được nêu khi đúng rule reclaim đã backtest; không dùng R5 PRE_OPEN để tự cho phép scalp.")
            return self._limit_answer_lines(lines, max_lines=7), f

        action_lines = self._intraday_action_lines(context, p, snapshot, requested_side=requested)
        if action_lines:
            mark_decision(action_lines)
            lines.append(
                f"Kèo hệ đang xét: {self._plan_label(p)} {side} {_fmt(entry)} → {_fmt(target)}; {self._compact_volume_text(p)}."
            )
            lines.append(self._r5_execution_authority_line(context, side))
            snap_line = self._snapshot_line(snapshot)
            if snap_line:
                lines.append(snap_line)
            selected_actions = list(action_lines)
            if any("scalp ngược nhịp" in x for x in action_lines):
                picked: list[str] = []
                if action_lines:
                    picked.append(action_lines[0])
                for token in ("có thể LONG scalp ngược nhịp", "có thể SHORT scalp ngược nhịp", "Mốc xử lý", "Nếu mất lại Open", "Nếu vượt lại Open"):
                    match = next((x for x in action_lines if token in x), None)
                    if match and match not in picked:
                        picked.append(match)
                selected_actions = picked[:4]
            lines.extend(selected_actions)
            if warning_lines and not warning_seen:
                lines.append(warning_lines[0])
                if str((context.get("r5_control") or {}).get("current_action") or "").upper() == "CANCEL":
                    lines.append("R5 CANCEL hiện tại có quyền ưu tiên cao hơn evidence lịch sử: hôm nay vẫn NO TRADE cho tới khi outputs phát action mới.")
                else:
                    if snapshot.is_completed_bar:
                        pass  # Action lines already state exact NO FILL/NO TRADE result.
                    else:
                        summary = self._warning_action_summary(self._dominant_warning(context, p), p)
                        if summary:
                            lines.append(summary)
                        if str((context.get("r5_control") or {}).get("current_action") or "").upper() == "PRE_OPEN":
                            lines.append("R5 hiện mới PRE_OPEN: đây là bản đồ xử lý theo OHLC, chưa phải lệnh chính thức; chờ base cập nhật KEEP/CANCEL/FLIP_HINT.")
            return self._limit_answer_lines(lines, max_lines=7), f

        if warning_lines:
            lines.extend(warning_lines)

        if side == "SHORT":
            if entry is not None and price < float(entry):
                gap = float(entry) - float(price)
                lines.append(f"Giá {_fmt(price)} đang thấp hơn vùng SHORT {_fmt(entry)} khoảng {_fmt(gap)} điểm: không SHORT đuổi.")
                if target is not None and price <= float(target):
                    lines.append(f"Target {_fmt(target)} đã bị đi qua trước khi entry được chạm, nên kèo frozen chưa được tính thắng; ưu tiên đứng ngoài, chờ hồi lên vùng entry hoặc chỉ cân nhắc LONG hồi nhỏ nếu có xác nhận.")
                else:
                    lines.append(f"Kế hoạch thuận hệ là chờ hồi về {_fmt(entry)} rồi mới đánh giá SHORT; target frozen {_fmt(target)}.")
            else:
                lines.append(f"Giá {_fmt(price)} đã vào/qua vùng chờ SHORT {_fmt(entry)}; chỉ cân nhắc lệnh nếu nhịp tăng dừng lại, không bán chỉ vì vừa chạm một con số.")
                lines.append(f"Mục tiêu frozen là {_fmt(target)}; nếu giá giữ vững trên entry thì giảm độ tin cậy, không bình quân SHORT.")
        elif side == "LONG":
            if entry is not None and price > float(entry):
                gap = float(price) - float(entry)
                lines.append(f"Giá {_fmt(price)} đang cao hơn vùng LONG {_fmt(entry)} khoảng {_fmt(gap)} điểm: không LONG đuổi.")
                if target is not None and price >= float(target):
                    lines.append(f"Target {_fmt(target)} đã bị đi qua trước khi entry được chạm, nên kèo frozen chưa được tính thắng; ưu tiên đứng ngoài hoặc chỉ cân nhắc SHORT hồi nhỏ nếu có xác nhận.")
                else:
                    lines.append(f"Kế hoạch thuận hệ là chờ điều chỉnh về {_fmt(entry)} rồi mới đánh giá LONG; target frozen {_fmt(target)}.")
            else:
                lines.append(f"Giá {_fmt(price)} đã vào/qua vùng chờ LONG {_fmt(entry)}; chỉ cân nhắc lệnh khi nhịp giảm dừng lại và có reclaim, không mua chỉ vì vừa chạm một con số.")
                lines.append(f"Mục tiêu frozen là {_fmt(target)}; nếu giá giữ vững dưới entry thì giảm độ tin cậy, không bình quân LONG.")
        else:
            lines.append("Outputs chưa xác định được hướng LONG/SHORT thống nhất; không nên ép một kèo chính.")

        if snapshot.session_open is not None:
            rel = float(price) - float(snapshot.session_open)
            lines.append(f"So với giá mở cửa {_fmt(snapshot.session_open)}, giá hiện {'cao hơn' if rel>0 else 'thấp hơn' if rel<0 else 'đúng bằng'} {_fmt(abs(rel))} điểm; đây là dữ kiện intraday, không tự nó đảo hướng frozen.")
        elif intent in {"SCENARIO", "OPEN_SCENARIO", "CURRENT_PLAN", "RISK"}:
            lines.append("Thiếu Open–High–Low nên chưa kết luận được giá đang ở đáy, giữa hay đỉnh biên phiên.")

        if f["warnings"] and not warning_lines:
            lines.append("Outputs có cảnh báo; chỉ dùng entry/target đã hiệu chỉnh và giảm mức chủ động nếu diễn biến thực tế trái hướng hệ.")
        return self._limit_answer_lines(lines, max_lines=7), f

    def _engine_fill_audit_lines(
        self,
        context: dict[str, Any],
        snapshot: SessionSnapshot,
        *,
        explain_reference: bool = False,
    ) -> list[str]:
        """Recompute fills from trusted OHLC and every active engine entry.

        Never trusts conversational position memory. This is the final reconciliation path for
        questions such as "tóm lại engine nào khớp".
        """
        plans = list(context.get("active_plans", []))
        high = float(snapshot.session_high) if snapshot.session_high is not None else None
        low = float(snapshot.session_low) if snapshot.session_low is not None else None
        if not plans:
            return ["Outputs không có kế hoạch active cho ngày đang xét; không thể ghi nhận engine nào khớp lệnh."]
        if high is None or low is None:
            return ["Chưa có đủ High–Low của phiên nên chưa thể kết luận engine nào khớp. Hãy gửi O–H–L–P hoặc dòng OHLC hoàn chỉnh."]

        rows: list[tuple[dict[str, Any], bool, float]] = []
        for plan in plans:
            entry = plan.get("operational_entry")
            direction = str(plan.get("direction") or "").upper()
            if entry is None or direction not in {"LONG", "SHORT"}:
                continue
            entry_f = float(entry)
            touched = high + 1e-9 >= entry_f if direction == "SHORT" else low - 1e-9 <= entry_f
            miss = max(0.0, entry_f - high) if direction == "SHORT" else max(0.0, low - entry_f)
            rows.append((plan, touched, miss))

        touched_rows = [x for x in rows if x[1]]
        authorized_rows: list[tuple[dict[str, Any], bool, float]] = []
        for row in touched_rows:
            action = str(row[0].get("r5_action") or "").upper()
            phase = str(row[0].get("phase") or "").upper()
            # Empty R5/PRE means price touched only, not an official R5-authorized trade.
            if action not in {"CANCEL"} and phase not in {"PRE", "PRE_OHLCV"}:
                authorized_rows.append(row)

        if not touched_rows:
            lines = [f"Tóm lại: không engine nào chạm được vùng vào trong phiên {context.get('as_of') or ''}; vì vậy không có lệnh nào khớp.".strip()]
        else:
            names = ", ".join(self._plan_label(x[0]) for x in touched_rows)
            lines = [f"Theo biên giá H {_fmt(high)} / L {_fmt(low)}, các vùng vào đã được giá chạm: {names}."]
            if not authorized_rows:
                lines.append("Nhưng các row này vẫn ở PRE/PRE_OHLCV hoặc chưa có quyền R5 cuối; đây chỉ là chạm giá, chưa được gọi là lệnh vận hành chính thức.")

        for plan, touched, miss in rows:
            label = self._plan_label(plan)
            direction = str(plan.get("direction") or "").upper()
            entry = float(plan.get("operational_entry"))
            target = plan.get("operational_target")
            profile = str(plan.get("profile") or "")
            phase = str(plan.get("phase") or "")
            r5 = str(plan.get("r5_action") or "")
            suffix = []
            if phase:
                suffix.append(phase)
            if r5:
                suffix.append(f"R5 {r5}")
            state = "/".join(suffix) or "chưa có action R5"
            if touched:
                lines.append(f"- {label}: giá đã chạm entry {direction} {_fmt(entry)}; trạng thái {state}. Target {_fmt(target)} chỉ xét sau fill và đúng thứ tự intraday.")
            else:
                extreme = high if direction == "SHORT" else low
                extreme_name = "High" if direction == "SHORT" else "Low"
                lines.append(f"- {label}: KHÔNG FILL — entry {direction} {_fmt(entry)}, {extreme_name} phiên {_fmt(extreme)}, còn thiếu {_fmt(miss)} điểm.")

        has_ptkt = any(p.get("engine") == "gpt_simptkt" for p in plans)
        if not has_ptkt:
            lines.append("- SimPTKT: không có row active trong outputs của ngày này, nên không có lệnh để xét fill.")

        target_prepassed = False
        for plan, touched, _ in rows:
            if touched:
                continue
            target = plan.get("operational_target")
            direction = str(plan.get("direction") or "").upper()
            if target is None:
                continue
            tf = float(target)
            if (direction == "SHORT" and low <= tf) or (direction == "LONG" and high >= tf):
                target_prepassed = True
                break
        if target_prepassed:
            lines.append("Giá có đi qua một số target trước khi entry được chạm; đó không tạo PnL và không được tính là kèo thắng.")
        if explain_reference:
            lines.append("Mốc 1.835 được nhắc lại trong câu hỏi chỉ là entry tham chiếu; nó không phải dữ liệu giá mới và không được phép sửa High 1.834,9 thành 1.835,0.")
        return self._limit_answer_lines(lines, max_lines=8)


    @staticmethod
    def _is_trader_execution_question(question: str) -> bool:
        q = SmartAdvisor._fold(question)
        terms = [
            "sl ", "stoploss", "stop loss", "catloss", "cutloss", "cut loss",
            "soc atc", "short atc", "long atc", "vao atc", "dong atc",
            "long nhe", "short nhe", "long duoc ko", "short duoc ko",
            "dinh long", "dinh short", "muon long", "muon short",
            "khong soc kip", "khong short kip", "khong long kip",
            "nay khop lenh", "hom nay khop", "co keo gi", "dang co keo gi",
        ]
        if any(t in q for t in terms):
            return True
        return bool(re.search(r"\b(?:long|short|soc)\b.*\b\d{2,4}(?:[.,]\d+)?\b", q))

    @staticmethod
    def _extract_proposed_entry(question: str, side: str | None) -> float | None:
        if not side:
            return None
        q = SmartAdvisor._fold(question)
        pats = [
            rf"{side.lower()}\s*(?:o|tai|gia)?\s*(\d{{3,4}}(?:[.,]\d+)?)",
            rf"(?:vao|entry)\s*{side.lower()}\s*(?:o|tai|gia)?\s*(\d{{3,4}}(?:[.,]\d+)?)",
        ]
        for pat in pats:
            m = re.search(pat, q)
            if m:
                vals = SmartAdvisor._parse_numbers(m.group(1))
                if vals and 500 <= vals[0] <= 5000:
                    return vals[0]
        return None

    @staticmethod
    def _extract_proposed_stop(question: str) -> float | None:
        q = SmartAdvisor._fold(question)
        pats = [
            r"(?:sl|stoploss|stop loss|catloss|cutloss|cut loss)\s*(?:o|tai|gia)?\s*(\d{2,4}(?:[.,]\d+)?)",
            r"(?:cat|cut)\s*(?:o|tai)?\s*(\d{2,4}(?:[.,]\d+)?)",
        ]
        for pat in pats:
            m = re.search(pat, q)
            if m:
                vals = SmartAdvisor._parse_numbers(m.group(1))
                if vals:
                    v = vals[0]
                    # Trader shorthand: at 1833, "SL 28" means 1828.
                    if v < 100 and v >= 0:
                        return v
                    if 500 <= v <= 5000:
                        return v
        return None

    @staticmethod
    def _expand_shorthand_level(level: float | None, reference: float | None) -> float | None:
        if level is None or reference is None or level >= 100:
            return level
        base = int(float(reference) // 100) * 100
        candidate = base + level
        if abs(candidate - float(reference)) > 60:
            candidate += 100 if candidate < reference else -100
        return float(candidate)

    @classmethod
    def _extract_narrative_path(cls, question: str, reference: float | None) -> dict[str, float | None]:
        q = cls._fold(question)
        out: dict[str, float | None] = {"high": None, "low": None, "current": None}
        up = re.search(r"(?:len|vuot|cham)\s*(\d{2,4}(?:[.,]\d+)?)", q)
        down = re.search(r"(?:gay lai|roi lai|xuong lai|ve lai|mat lai)\s*(\d{2,4}(?:[.,]\d+)?)", q)
        def parse(m):
            if not m:
                return None
            vals = cls._parse_numbers(m.group(1))
            if not vals:
                return None
            return cls._expand_shorthand_level(vals[0], reference)
        out["high"] = parse(up)
        out["current"] = parse(down)
        out["low"] = out["current"]
        return out

    def _trader_execution_lines(
        self,
        question: str,
        context: dict[str, Any],
        focus_plan: dict[str, Any] | None,
        memory: ConversationMemory,
        snapshot: SessionSnapshot,
    ) -> list[str]:
        q = self._fold(question)
        primary = focus_plan or self._primary_plan(context)
        if not primary:
            return ["Hôm nay outputs chưa có kèo frozen hợp lệ; không nên tự dựng LONG/SHORT chỉ từ cảm giác giá."]
        system_side = str(primary.get("direction") or "").upper()
        entry = primary.get("operational_entry")
        target = primary.get("operational_target")
        requested = self._detect_requested_side(question)
        price = snapshot.live_price
        narrative = self._extract_narrative_path(question, price or entry)
        local_high = narrative.get("high") if narrative.get("high") is not None else snapshot.session_high
        local_low = narrative.get("low") if narrative.get("low") is not None else snapshot.session_low
        local_price = narrative.get("current") if narrative.get("current") is not None else price
        proposed_entry = self._extract_proposed_entry(question, requested)
        if proposed_entry is None and requested and price is not None and any(x in q for x in ["muon long", "muon short", "long qua", "short qua", "long nhe", "short nhe"]):
            proposed_entry = float(local_price)
        raw_stop = self._extract_proposed_stop(question)
        stop = self._expand_shorthand_level(raw_stop, proposed_entry or price)
        lines: list[str] = []

        emotional = any(x in q for x in ["chan qua", "dinh cutloss", "dinh catloss", "vua cat", "khong soc kip", "khong short kip", "tiec"])
        if emotional:
            lines.append("Đừng dùng lệnh kế tiếp để gỡ cú cutloss hoặc cú bỏ lỡ; quyết định mới phải đứng độc lập theo OHLC và kèo frozen hiện tại.")

        if any(x in q for x in ["co keo gi", "dang co keo gi", "nay khop lenh", "hom nay khop", "khop lenh ko"]):
            lines.extend(self._engine_fill_audit_lines(context, snapshot)[:4])
            lines.append(f"Kèo chính còn hiệu lực: {system_side} {_fmt(entry)} → {_fmt(target)}; chỉ tính khớp khi entry được chạm đúng thứ tự.")
            return self._limit_answer_lines(lines, max_lines=7)

        if "atc" in q:
            if requested is None:
                requested = "SHORT" if "soc" in q or "short" in q else ("LONG" if "long" in q else None)
            if requested != system_side:
                lines.append(f"Không nên {requested or 'vào lệnh'} ATC chỉ vì tiếc nhịp; kèo frozen hôm nay là {system_side} {_fmt(entry)} → {_fmt(target)}.")
            elif local_high is None or local_low is None or local_price is None:
                lines.append(f"{requested} ATC chưa đủ căn cứ: cần High–Low và giá đóng/giá hiện tại để biết entry {_fmt(entry)} đã được chạm rồi xác nhận hay chưa.")
            else:
                touched = (system_side == "SHORT" and local_high >= float(entry)) or (system_side == "LONG" and local_low <= float(entry))
                confirmed = (system_side == "SHORT" and local_price < float(entry)) or (system_side == "LONG" and local_price > float(entry))
                if touched and confirmed:
                    lines.append(f"{requested} ATC phù hợp hướng kèo vì đường giá đã chạm entry {_fmt(entry)} rồi lùi về {_fmt(local_price)} xác nhận lại đúng phía; vẫn vào theo ladder, không all-in ATC.")
                elif not touched:
                    lines.append(f"Không {requested} ATC: entry {_fmt(entry)} chưa được chạm trong phiên, nên ATC sẽ là đuổi lệnh chứ không phải thực thi đúng kèo.")
                else:
                    lines.append(f"Chưa {requested} ATC: entry đã chạm nhưng giá {_fmt(local_price)} chưa xác nhận lại đúng phía {_fmt(entry)}; kèo còn ở trạng thái chờ xác nhận.")
            lines.append(self._r5_execution_authority_line(context, system_side))
            return self._limit_answer_lines(lines, max_lines=6)

        if requested:
            trade_price = proposed_entry if proposed_entry is not None else local_price
            if trade_price is None:
                return [f"Muốn xét {requested}, hãy cho giá hiện tại hoặc mức định vào cùng O–H–L; chưa có giá thì chưa thể biết đang bắt đáy hay đuổi giá."]
            relation = "thuận" if requested == system_side else "ngược"
            lines.append(f"Mức {_fmt(trade_price)} là ý tưởng {requested} {relation} kèo frozen {system_side} {_fmt(entry)} → {_fmt(target)}.")
            if requested != system_side:
                if requested == "LONG" and float(trade_price) >= float(entry):
                    lines.append(f"Không LONG mới tại {_fmt(trade_price)}: đã tới/qua vùng hệ chờ SHORT {_fmt(entry)}, dư địa bridge không còn và rủi ro bị rejection cao.")
                elif requested == "SHORT" and float(trade_price) <= float(entry):
                    lines.append(f"Không SHORT đuổi tại {_fmt(trade_price)}: đang dưới vùng hệ chờ SHORT {_fmt(entry)}; phải chờ hồi lên entry rồi rejection mới đúng kèo.")
                else:
                    room = abs(float(entry) - float(trade_price))
                    lines.append(f"Có thể chỉ xem là scalp ngược nhịp với size nhỏ; dư địa tới vùng đóng bắt buộc {_fmt(entry)} còn {_fmt(room)} điểm, chạm vùng đó phải thoát chứ không giữ hy vọng.")
            else:
                if requested == "SHORT":
                    if snapshot.session_high is not None and snapshot.session_high >= float(entry) and float(trade_price) < float(entry):
                        lines.append(f"Có thể SHORT theo kèo: High đã chạm/xuyên {_fmt(entry)} và giá vào {_fmt(trade_price)} nằm lại dưới entry, tức có rejection.")
                    elif float(trade_price) < float(entry):
                        lines.append(f"Chưa SHORT tại {_fmt(trade_price)}: thấp hơn entry {_fmt(entry)} nhưng chưa chứng minh đã chạm rồi rejection; bán ở đây dễ thành SHORT đuổi.")
                    else:
                        lines.append(f"Chưa SHORT khi giá còn ở/trên {_fmt(entry)}; cần mất lại entry sau khi chạm vùng cao.")
                else:
                    if snapshot.session_low is not None and snapshot.session_low <= float(entry) and float(trade_price) > float(entry):
                        lines.append(f"Có thể LONG theo kèo: Low đã chạm/xuyên {_fmt(entry)} và giá vào {_fmt(trade_price)} lấy lại trên entry.")
                    elif float(trade_price) > float(entry):
                        lines.append(f"Chưa LONG tại {_fmt(trade_price)}: cao hơn entry {_fmt(entry)} nhưng chưa chứng minh đã test rồi reclaim; mua ở đây có thể là đuổi giá.")
                    else:
                        lines.append(f"Chưa LONG khi giá còn ở/dưới {_fmt(entry)}; cần reclaim entry sau khi test vùng thấp.")

            if stop is not None:
                risk = (float(trade_price) - float(stop)) if requested == "LONG" else (float(stop) - float(trade_price))
                reward_anchor = (float(entry) - float(trade_price)) if requested == "LONG" and requested != system_side else ((float(trade_price) - float(entry)) if requested == "SHORT" and requested != system_side else abs(float(target) - float(trade_price)))
                if risk <= 0:
                    lines.append(f"SL {_fmt(stop)} nằm sai phía với lệnh {requested}; cấu trúc này không hợp lệ.")
                else:
                    rr = reward_anchor / risk if risk > 0 else 0
                    lines.append(f"SL {_fmt(stop)} tương đương rủi ro {_fmt(risk)} điểm; lợi thế tới mốc hệ gần nhất khoảng {_fmt(reward_anchor)} điểm, R/R xấp xỉ {rr:.2f}.")
                    if rr < 1.0:
                        lines.append("R/R dưới 1: không đáng vào mới, nhất là sau một cú cutloss; hoặc chờ giá tốt hơn, hoặc bỏ lệnh.")
                    elif rr < 1.5:
                        lines.append("R/R chỉ trung bình: chỉ phù hợp size nhỏ và phải có OHLC xác nhận, không vào vì sợ lỡ nhịp.")
            elif any(x in q for x in ["sl", "stop", "cutloss", "catloss"]):
                lines.append("Chưa đọc được mức SL cụ thể; hãy ghi đầy đủ kiểu 'LONG 1833, SL 1828'.")
            lines.append(self._r5_execution_authority_line(context, system_side))
            return self._limit_answer_lines(lines, max_lines=7)

        return self._system_playbook_lines(context, snapshot)

    @staticmethod
    def _parse_ladder_rule(plan: dict[str, Any]) -> dict[str, float] | None:
        raw = str(plan.get("volume_rule") or "").upper()
        m = re.search(r"BASE_([0-9.]+)_ADD_([0-9.]+)_PER_([0-9.]+)PT_MAX_([0-9.]+)", raw)
        if not m:
            return None
        return {"base": float(m.group(1)), "add": float(m.group(2)), "step": float(m.group(3)), "max": float(m.group(4))}

    @staticmethod
    def _level_mentions(context: dict[str, Any]) -> list[dict[str, Any]]:
        mentions: list[dict[str, Any]] = []
        for p in context.get("all_forward_plans", context.get("active_plans", [])):
            for role, key in (("ENTRY", "operational_entry"), ("TARGET", "operational_target")):
                v = p.get(key)
                if v is None:
                    continue
                mentions.append({
                    "value": float(v), "role": role, "date": p.get("date", ""),
                    "engine": p.get("engine", ""), "profile": p.get("profile", ""),
                    "horizon": p.get("horizon", ""), "direction": str(p.get("direction") or "").upper(),
                })
        return mentions

    def _answer_level_execution(self, context: dict[str, Any], snapshot: SessionSnapshot, question: str) -> list[str]:
        qf = self._fold(question)
        active = list(context.get("active_plans", []))
        if not active:
            return ["Database không có kèo FORWARD hiện hành; không dựng nấc giá hay bình quân."]
        primary = self._primary_plan(context) or active[0]
        side = str(primary.get("direction") or "").upper()
        entry = float(primary.get("operational_entry")) if primary.get("operational_entry") is not None else None
        target = float(primary.get("operational_target")) if primary.get("operational_target") is not None else None
        mentions = self._level_mentions(context)
        clusters: list[tuple[float,list[dict[str,Any]]]] = []
        for m in sorted(mentions, key=lambda x:x["value"]):
            for i,(lv,items) in enumerate(clusters):
                if abs(lv-m["value"]) <= 0.40:
                    items.append(m); clusters[i]=(sum(x["value"] for x in items)/len(items),items); break
            else:
                clusters.append((m["value"],[m]))
        shared = [(lv,items) for lv,items in clusters if len({(x["engine"],x["profile"],x["horizon"],x["role"]) for x in items}) >= 2]
        shared.sort(key=lambda z:(-len(z[1]), z[0]))
        lines=[]
        if shared:
            pieces=[]
            for lv,items in shared[:4]:
                labels=[]
                for x in items:
                    name=_engine_display(x["engine"],x["profile"])
                    hz=(" "+x["horizon"]) if x["horizon"] else ""
                    labels.append(f"{name}{hz} {x['role'].lower()}")
                pieces.append(f"{_fmt(lv)} ({', '.join(labels)})")
            lines.append("Các nấc giao nhau trong database: " + "; ".join(pieces) + ".")
        else:
            lines.append("Database hiện không có mức entry/target giao nhau rõ giữa nhiều engine/horizon.")

        ladder_plan = next((p for p in active if p.get("engine")=="gpt_simcarrry6" and self._parse_ladder_rule(p)), primary)
        rule = self._parse_ladder_rule(ladder_plan)
        if rule and entry is not None:
            max_adds = int(round((rule["max"]-rule["base"])/rule["add"])) if rule["add"]>0 else 0
            if side=="SHORT":
                ladder=[entry+i*rule["step"] for i in range(max_adds+1)]
            else:
                ladder=[entry-i*rule["step"] for i in range(max_adds+1)]
            lines.append(f"Ladder gốc của {self._plan_label(ladder_plan)}: {rule['base']:.2f} tại {_fmt(ladder[0])}, thêm {rule['add']:.2f} mỗi {rule['step']:.1f} điểm bất lợi, tối đa {rule['max']:.2f}; nấc cuối khoảng {_fmt(ladder[-1])}.")
        else:
            ladder=[]
            lines.append("Outputs không ghi ladder định lượng cho engine đang hỏi; không tự bịa nấc bình quân.")

        price=snapshot.live_price
        if price is None:
            vals=[v for v in self._parse_numbers(question) if 1000 <= v <= 3000]
            price=vals[-1] if vals else None
        op=snapshot.session_open; hi=snapshot.session_high; lo=snapshot.session_low
        if price is not None and entry is not None and target is not None:
            if side=="SHORT":
                if hi is not None and hi>=entry and price<entry:
                    lines.append(f"Giá đã chạm/xuyên entry {_fmt(entry)} rồi rơi lại dưới: đây là rejection/false breakout có lợi cho SHORT; chỉ các nấc đã thực sự chạm mới được tính fill.")
                elif price>entry:
                    lines.append(f"Giá {_fmt(price)} đang trên entry SHORT {_fmt(entry)}: chỉ tăng vị thế theo đúng ladder và cap; không cộng vượt cap vì breakout chưa gãy lại.")
                elif hi is not None and hi<entry:
                    lines.append(f"High {_fmt(hi)} chưa chạm entry {_fmt(entry)}: chưa fill, không được bình quân hay ghi thắng dù giá đã đi dưới target.")
                if op is not None and op<target:
                    lines.append(f"Gap down dưới target {_fmt(target)} trước fill: lợi thế đã đi qua; không SHORT đuổi và không bình quân xuống. Chỉ xét LONG reclaim nếu đúng rule đã backtest.")
                if op is not None and ladder and op>=ladder[-1]:
                    lines.append(f"Gap up tới/trên nấc cuối {_fmt(ladder[-1])}: cap native vẫn {rule['max']:.2f}; outputs không quy định bắt bù toàn bộ nấc bị nhảy nên không tự động all-in các nấc bỏ qua.")
            elif side=="LONG":
                if lo is not None and lo<=entry and price>entry:
                    lines.append(f"Giá đã chạm/xuyên entry {_fmt(entry)} rồi lấy lại trên: reclaim có lợi cho LONG; chỉ các nấc đã thực sự chạm mới tính fill.")
                elif price<entry:
                    lines.append(f"Giá {_fmt(price)} đang dưới entry LONG {_fmt(entry)}: chỉ tăng theo ladder nếu outputs ghi rõ; không bình quân vô hạn khi breakdown chưa reclaim.")
                if op is not None and op>target:
                    lines.append(f"Gap up trên target {_fmt(target)} trước fill: không LONG đuổi; target xuất hiện trước entry không được tính thắng.")
        asks_avg=any(x in qf for x in ["binh quan","trung binh gia","tang khoi luong","tang size","them vi the","tung nac","ladder"])
        if asks_avg and rule and ladder and price is not None:
            if side=="SHORT": touched=[x for x in ladder if price>=x or (hi is not None and hi>=x)]
            else: touched=[x for x in ladder if price<=x or (lo is not None and lo<=x)]
            total=min(rule["max"], rule["base"] + max(0,len(touched)-1)*rule["add"]) if touched else 0.0
            if not touched:
                lines.append("Chưa có nấc nào được chạm: size hợp lệ hiện tại 0, không bình quân trước fill.")
            else:
                lines.append(f"Theo ladder native, đã chạm {len(touched)} nấc ({', '.join(_fmt(x) for x in touched)}), tổng size tối đa tương ứng {total:.2f}; không thêm ở phía có lợi sau entry.")
        lines.append("Ưu tiên mức chung để ra quyết định, nhưng size vẫn theo rule của từng engine; không cộng chồng hai profile engine5 như hai tín hiệu độc lập.")
        return self._limit_answer_lines(lines, max_lines=9)

    def _answer_plan_chart(self, context: dict[str, Any], snapshot: SessionSnapshot) -> list[str]:
        rows = list(context.get("all_forward_plans") or context.get("forward_plans") or [])
        if not rows:
            return [
                "Không có row FORWARD hợp lệ để đặt lên chart.",
                "Vui lòng liên hệ beefx.com để được cung cấp hoặc cập nhật cơ sở dữ liệu mới nhất.",
            ]
        rows = sorted(rows, key=lambda r: (r.get("date_ts") or pd.Timestamp.min, self._plan_label(r)))
        lines = [f"Đã đặt {len(rows)} kèo forward hiện có trong database lên chart:"]
        for r in rows:
            direction = str(r.get("direction") or "NA").upper()
            entry = r.get("operational_entry") if r.get("operational_entry") is not None else r.get("entry")
            target = r.get("operational_target") if r.get("operational_target") is not None else r.get("target")
            status = str(r.get("risk_action") or r.get("status") or "NA")
            lines.append(f"- {r.get('date') or 'NA'} | {self._plan_label(r)} | {direction} {_fmt(entry)} → {_fmt(target)} | {status}.")
        snap = self._snapshot_line(snapshot)
        if snap:
            lines.append("Lớp giá khách cung cấp trên chart: " + snap.replace("OHLC khách cung cấp: ", ""))
        lines.append("Chart sẽ vẽ OHLC zoom, toàn bộ Entry/TP forward và band Expected Low/Center/Expected High theo từng engine/horizon.")
        lines.append("Band lịch sử được tính từ BEST_ENGINE_RECENT_TRADES.tsv và BEST_ENGINE_CHART_LAST3_TRADES.tsv; nếu engine thiếu mẫu, chart ghi SAME_ROW_SPAN thay vì bịa độ tin cậy.")
        return self._limit_answer_lines(lines, max_lines=12)

    def _source_disclosure_line(self, context: dict[str, Any], intent: str) -> str:
        perf_intents = {"RECENT_PERFORMANCE", "RECENT_PERFORMANCE_REVERSE", "ENGINE_PERFORMANCE", "ADVICE_VALIDATION", "PERFORMANCE_FULL_AUDIT", "HISTORY"}
        if intent == "PERFORMANCE_AND_TOMORROW":
            fwd = Path(context.get("forward_database") or context.get("source_file") or "outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv").name
            hist = Path(context.get("history_database") or "outputs/BEST_ENGINE_RECENT_TRADES.tsv").name
            return f"Nguồn database: forward={fwd}; history={hist}; không trộn row forward vào PnL lịch sử."
        if intent == "CHART_COMBINED":
            fwd = Path(context.get("forward_database") or context.get("source_file") or "outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv").name
            hist = Path(context.get("history_database") or "outputs/BEST_ENGINE_RECENT_TRADES.tsv").name
            return f"Nguồn database: forecast={fwd}; history={hist}; forecast bands còn dùng BEST_ENGINE_CHART_LAST3_TRADES.tsv để hiệu chỉnh theo lịch sử đúng engine."
        if intent == "MISSING_DATA":
            fwd = Path(context.get("forward_database") or context.get("source_file") or "outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv").name
            hist = Path(context.get("history_database") or "outputs/BEST_ENGINE_RECENT_TRADES.tsv").name
            return f"Nguồn đã kiểm tra: forward={fwd}; history={hist}. Thiếu dữ liệu thì liên hệ beefx.com để cập nhật CSDL."
        if intent in perf_intents:
            src = Path(context.get("history_database") or "outputs/BEST_ENGINE_RECENT_TRADES.tsv").name
            count = len(context.get("recent_history", []))
            return f"Nguồn database: {src} | phạm vi nạp: {count} row lịch sử; chỉ kết luận trong phạm vi dữ liệu này."
        src = Path(context.get("source_file") or context.get("forward_database") or "outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv").name
        count = len(context.get("all_forward_plans", context.get("forward_plans", [])))
        return f"Nguồn database: {src} | forward đang nạp: {count} row; không suy diễn ngoài outputs."

    def _compose_focused(
        self,
        question: str,
        intent: str,
        context: dict[str, Any],
        focus_plan: dict[str, Any] | None,
        memory: ConversationMemory,
        snapshot: SessionSnapshot,
    ) -> tuple[str, dict[str, Any]]:
        complexity = question_complexity(question)
        max_lines = 3 if complexity["score"] <= 1 else (5 if complexity["score"] <= 3 else 7)
        if intent in {"LEVEL_EXECUTION", "BRIDGE_LONG_TO_SHORT", "BRIDGE_SHORT_TO_LONG", "COUNTERTREND_PLAN", "SIDE_PLAN", "ENGINE_FILL_AUDIT", "ENGINE_PLAYBOOK", "SYSTEM_PLAYBOOK", "SCENARIO", "OPEN_SCENARIO", "FILL_STATUS", "ACCOUNTABILITY_RECOVERY", "R5_GUIDANCE"}:
            max_lines = 7
        if intent == "TOP_ENGINES":
            max_lines = 16
        if intent in {"RECENT_PERFORMANCE", "RECENT_PERFORMANCE_REVERSE"}:
            max_lines = 14
        if intent in {"ENGINE_PERFORMANCE", "ADVICE_VALIDATION", "PERFORMANCE_FULL_AUDIT"}:
            max_lines = 10
        if intent == "PERFORMANCE_FULL_AUDIT":
            max_lines = 22
        if intent in {"TODAY_PLANS", "FORECAST_RANGE", "PLAN_CHART"}:
            max_lines = 12
        if intent == "CHART_COMBINED":
            max_lines = 24
        if self._dominant_warning(context, focus_plan or self._primary_plan(context)):
            max_lines = max(max_lines, 5)
        reasoning_intents = {"ACCOUNTABILITY_RECOVERY", "R5_GUIDANCE", "FORECAST_CHART", "FORECAST_RANGE", "COUNTERTREND_PLAN", "SIDE_PLAN", "OPEN_SCENARIO", "SCENARIO", "FILL_STATUS", "CURRENT_PLAN", "PRIORITY", "CONSENSUS", "WHY", "RISK", "QUALITY", "COMPARE", "CHANGE"}
        reasoning_frame = None
        if intent == "CHART_COMBINED":
            lines = self._answer_plan_chart(context, snapshot) + ["--- Hiệu suất lịch sử ---"] + self._answer_engine_performance(context, question)
        elif intent == "PLAN_CHART":
            lines = self._answer_plan_chart(context, snapshot)
        elif intent == "LEVEL_EXECUTION":
            lines = self._answer_level_execution(context, snapshot, question)
        elif intent == "BRIDGE_LONG_TO_SHORT":
            lines = self._answer_bridge_long_to_short(context, focus_plan, snapshot)
        elif intent == "BRIDGE_SHORT_TO_LONG":
            lines = self._answer_bridge_short_to_long(context, focus_plan, snapshot)
        elif self._is_trader_execution_question(question) and intent not in {"ACCOUNTABILITY_RECOVERY", "TOP_ENGINES", "HISTORY", "LATEST_DATE", "TOMORROW_PLAN", "TODAY_PLANS", "FORWARD_COUNT", "DATA_SOURCE", "FORECAST_RANGE", "ENGINE_PLAYBOOK", "RECENT_PERFORMANCE", "RECENT_PERFORMANCE_REVERSE", "ENGINE_PERFORMANCE", "ADVICE_VALIDATION", "PLAN_CHART"}:
            lines = self._trader_execution_lines(question, context, focus_plan, memory, snapshot)
            reasoning_frame = self._reasoning_frame(question, context, focus_plan, memory, snapshot)
        elif intent == "ENGINE_FILL_AUDIT":
            qf = self._fold(question)
            lines = self._engine_fill_audit_lines(context, snapshot, explain_reference=("1835" in qf or "1834,9" in qf or "1834.9" in qf))
        elif intent == "ENGINE_PLAYBOOK":
            resolved = self._resolve_engine(question, memory)
            profile = ""
            engine = resolved
            if resolved.startswith("engine5:"):
                engine, profile = resolved.split(":", 1)
            qf = self._fold(question)
            lookup_only = not any(x in qf for x in ["giờ làm gì", "gio lam gi", "khớp", "khop", "fill", "giá", "gia", "ohlc"])
            clean_snapshot = SessionSnapshot() if lookup_only else snapshot
            lines = self._engine_playbook_lines(context, focus_plan, engine, profile, clean_snapshot)
        elif intent == "SYSTEM_PLAYBOOK":
            lines = self._system_playbook_lines(context, snapshot)
        elif intent == "FORECAST_RANGE":
            lines = self._answer_forecast_ranges(context, question)
        elif intent in reasoning_intents:
            lines, reasoning_frame = self._session_reasoning_answer(question, intent, context, focus_plan, memory, snapshot)
        elif intent == "EVIDENCE":
            lines = self._focused_evidence(context, focus_plan)
        elif intent == "TARGET":
            chosen = focus_plan or self._primary_plan(context)
            warning = self._dominant_warning(context, chosen)
            if chosen and chosen.get("operational_target") is not None:
                if warning and warning.get("id") == "BAND_DIRECTION_CONTRADICTION":
                    lines = [
                        f"Mốc chốt của kế hoạch đã sửa là {_fmt(chosen.get('operational_target'))}; không dùng mức chốt gốc bị đảo {_fmt(chosen.get('original_target'))}.",
                        "Mốc chốt chỉ được công nhận khi vùng vào đã khớp trước và giá chạm lại mốc đó sau khi có vị thế.",
                    ]
                else:
                    lines = [f"Mốc chốt của kèo đang hỏi: {_fmt(chosen.get('operational_target'))}."]
            else:
                lines = ["Kèo hiện tại chưa có mốc chốt hợp lệ."]
        elif intent == "MISSING_DATA":
            req_nums = self._parse_numbers(question)
            req_text = f" cho cửa sổ {int(req_nums[0])}" if req_nums else ""
            lines = [
                f"Chưa đủ dữ liệu trong database hiện tại để kết luận{req_text}; hệ không được phép tự bịa, nội suy hoặc lấy nguồn khác thay thế.",
                "Cần nêu rõ file, row hoặc trường đang thiếu. Vui lòng liên hệ beefx.com để được cung cấp hoặc cập nhật cơ sở dữ liệu mới nhất.",
            ]
        elif intent == "PERFORMANCE_AND_TOMORROW":
            lines = self._answer_tomorrow_plan(context) + ["--- Hiệu suất lịch sử ---"] + self._answer_recent_performance(context, question=question, include_reverse=False)
        elif intent == "PERFORMANCE_FULL_AUDIT":
            lines = self._answer_performance_full_audit(context, question)
        elif intent == "RECENT_PERFORMANCE":
            lines = self._answer_recent_performance(context, question=question, include_reverse=False)
        elif intent == "RECENT_PERFORMANCE_REVERSE":
            lines = self._answer_recent_performance(context, question=question, include_reverse=True)
        elif intent == "ENGINE_PERFORMANCE":
            lines = self._answer_engine_performance(context, question)
        elif intent == "ADVICE_VALIDATION":
            lines = self._answer_advice_validation(context, question)
        elif intent == "TOP_ENGINES":
            lines = self._answer_top_engines(context)
        elif intent == "TODAY_PLANS":
            lines = self._answer_today_plans(context)
        elif intent == "FORWARD_COUNT":
            lines = self._answer_forward_count(context)
        elif intent == "TOMORROW_PLAN":
            lines = self._answer_tomorrow_plan(context)
        elif intent == "LATEST_DATE":
            lines = self._answer_latest_date(context)
        elif intent == "DATA_SOURCE":
            lines = self._answer_data_source(context)
        elif intent == "HISTORY":
            lines = self._answer_history_top(context, question)
        elif intent == "FRESHNESS":
            fresh = context.get("freshness", {})
            lines = [f"Dữ liệu hoàn thành gần nhất: {fresh.get('latest_completed_ohlc_date') or 'chưa rõ'}.", f"Có {fresh.get('fresh_forward_count', 0)} kèo mới hợp lệ; kèo cũ đã bị loại tự động."]
        elif intent in {"WARNINGS", "RISK"}:
            chosen = focus_plan or self._primary_plan(context)
            lines = self._dominant_warning_lines(context, chosen, snapshot, evidence=True, action=True)
            if not lines:
                lines = ["Không có cảnh báo nghiêm trọng đang kích hoạt; vẫn chỉ vào đúng vùng giá hệ đưa ra."]
            elif any(k in self._norm(question) for k in ["có cửa", "đánh được", "nên đánh", "làm gì", "chơi kiểu gì"]):
                if snapshot.live_price is not None and chosen:
                    action = self._intraday_action_lines(context, chosen, snapshot, requested_side=self._detect_requested_side(question))
                    lines = action + lines[:2] if action else lines
                elif chosen:
                    side = str(chosen.get("direction") or "").upper()
                    opposite = "LONG" if side == "SHORT" else "SHORT"
                    lines.append(f"Cửa đánh: chờ {side} đúng vùng {_fmt(chosen.get('operational_entry'))} → {_fmt(chosen.get('operational_target'))}; nếu giá còn xa entry thì chỉ scalp {opposite} nhỏ khi Open–High–Low–giá live xác nhận.")
        elif intent == "WHY":
            primary = self._primary_plan(context)
            contradiction = any(w.get("id") == "BAND_DIRECTION_CONTRADICTION" for w in context.get("warnings", []))
            if contradiction and primary:
                lines = self._dominant_warning_lines(context, primary, snapshot, evidence=True, action=True)
            else:
                lines = self._focused_current_plan(context)
        elif intent in {"PRIORITY", "CONSENSUS", "CURRENT_PLAN", "COMPARE", "CHANGE"}:
            lines = self._focused_current_plan(context)
        else:
            lines = self._focused_current_plan(context)
        if complexity["compound"] and intent not in {"EVIDENCE", "DATA_SOURCE"}:
            primary = focus_plan or self._primary_plan(context)
            market = {}
            if primary and intent in {"CURRENT_PLAN", "WHY", "PRIORITY", "RISK", "COMPARE", "CHANGE", "SCENARIO", "OPEN_SCENARIO", "FILL_STATUS"}:
                lines.append(f"Kịch bản đổi ý: chỉ xem lại khi giá phá vùng chờ {_fmt(primary.get('operational_entry'))} theo hướng bất lợi hoặc xuất hiện trạng thái cancel/units 0.")
            if market.get("atr14"):
                lines.append(f"Biên dao động 14 phiên gần nhất khoảng {_fmt(market.get('atr14'))} điểm; tránh coi một nhịp nhỏ là đảo chiều chắc chắn.")
        source_line = self._source_disclosure_line(context, intent)
        if source_line not in lines:
            lines.append(source_line)
        lines = self._limit_answer_lines(lines, max_lines=max_lines + 1)
        structured = {
            "advisor_version": ADVISOR_VERSION,
            "intent": intent,
            "answer_style": "adaptive",
            "max_answer_lines": max_lines,
            "question_complexity": complexity,
            "as_of": context.get("as_of"),
            "focus_plan_key": _plan_key(focus_plan) if focus_plan else None,
            "consensus": context.get("consensus"),
            "warnings": context.get("warnings", []),
            "freshness": context.get("freshness"),
            "r5_control": context.get("r5_control"),
            "answer_sections": {"direct_answer": lines},
            "reasoning_frame": reasoning_frame,
        }
        return "\n".join(lines), structured

    def _compose_short(
        self,
        question: str,
        intent: str,
        context: dict[str, Any],
        focus_plan: dict[str, Any] | None,
        memory: ConversationMemory,
        snapshot: SessionSnapshot,
    ) -> tuple[str, dict[str, Any]]:
        if intent == "OPEN_SCENARIO":
            lines = self._concise_live_answer(context, snapshot, use_open=True)
        elif intent in {"SCENARIO", "FILL_STATUS"} and snapshot.live_price is not None:
            lines = self._concise_live_answer(context, snapshot, use_open=False)
        elif intent == "TARGET":
            lines = self._answer_target(context, focus_plan, memory, question)[:2]
        elif intent == "HISTORY":
            lines = self._answer_history_top(context, question)[:4]
        elif intent == "MISSING_DATA":
            req_nums = self._parse_numbers(question)
            req_text = f" cho cửa sổ {int(req_nums[0])}" if req_nums else ""
            lines = [
                f"Chưa đủ dữ liệu trong database hiện tại để kết luận{req_text}; hệ không được bịa hoặc lấy nguồn khác thay thế.",
                "Vui lòng liên hệ beefx.com để được cung cấp hoặc cập nhật cơ sở dữ liệu mới nhất.",
            ]
        elif intent == "PERFORMANCE_AND_TOMORROW":
            lines = (self._answer_tomorrow_plan(context) + ["--- Hiệu suất lịch sử ---"] + self._answer_recent_performance(context, question=question, include_reverse=False))[:16]
        elif intent == "PERFORMANCE_FULL_AUDIT":
            lines = self._answer_performance_full_audit(context, question)[:14]
        elif intent == "RECENT_PERFORMANCE":
            lines = self._answer_recent_performance(context, question=question, include_reverse=False)[:8]
        elif intent == "RECENT_PERFORMANCE_REVERSE":
            lines = self._answer_recent_performance(context, question=question, include_reverse=True)[:10]
        elif intent == "ENGINE_PERFORMANCE":
            lines = self._answer_engine_performance(context, question)[:8]
        elif intent == "ADVICE_VALIDATION":
            lines = self._answer_advice_validation(context, question)[:8]
        elif intent == "TOP_ENGINES":
            lines = self._answer_top_engines(context)[:7]
        elif intent == "LATEST_DATE":
            lines = self._answer_latest_date(context)[:7]
        elif intent == "FRESHNESS":
            lines = self._answer_freshness(context, question)[:3]
        elif intent == "EVIDENCE":
            severe = self._severe_warnings(context)
            if severe:
                w = severe[0]
                metrics = w.get("backtest_metrics", {}).get("full", {})
                lines = [self._compact_warning(w)]
                if metrics:
                    lines.append(
                        f"Backtest full: {int(metrics.get('raw_touched_trades',0))} mẫu, WR {metrics.get('raw_wr_pct',0):.1f}%, "
                        f"PnL {metrics.get('raw_pnl_points',0):+.1f} điểm."
                    )
            else:
                lines = ["Không có red flag backtest đang kích hoạt."]
        else:
            direct = self._direct_current_plan(context, focus_plan)
            lines = direct[:4]
        source_line = self._source_disclosure_line(context, intent)
        if source_line not in lines:
            lines.append(source_line)
        structured = {
            "advisor_version": ADVISOR_VERSION,
            "intent": intent,
            "answer_style": "short",
            "as_of": context.get("as_of"),
            "focus_plan_key": _plan_key(focus_plan) if focus_plan else None,
            "consensus": context.get("consensus"),
            "warnings": context.get("warnings", []),
            "freshness": context.get("freshness"),
            "answer_sections": {"direct_answer": lines},
        }
        return "\n".join(lines), structured

    def _compose(
        self,
        question: str,
        intent: str,
        context: dict[str, Any],
        focus_plan: dict[str, Any] | None,
        memory: ConversationMemory,
        snapshot: SessionSnapshot,
    ) -> tuple[str, dict[str, Any]]:
        return self._compose_focused(question, intent, context, focus_plan, memory, snapshot)

    def _compose_legacy_unused(
        self,
        question: str,
        intent: str,
        context: dict[str, Any],
        focus_plan: dict[str, Any] | None,
        memory: ConversationMemory,
        snapshot: SessionSnapshot,
    ) -> tuple[str, dict[str, Any]]:
        latest = context.get("latest_completed_ohlc") or {}
        header = f"BFXPS {context.get('as_of') or 'NA'}"
        if latest:
            header += f" | OHLC hoàn thành gần nhất {latest.get('date')}: O {_fmt(latest.get('open'))}, H {_fmt(latest.get('high'))}, L {_fmt(latest.get('low'))}, C {_fmt(latest.get('close'))}"
        lines = [header]

        severe = self._severe_warnings(context)
        if severe:
            lines.append("CẢNH BÁO ƯU TIÊN:")
            if intent in {"SCENARIO", "FILL_STATUS"} and snapshot.live_price is not None:
                for w in severe[:2]:
                    lines.append("- " + self._compact_warning(w))
            else:
                for w in severe:
                    lines.append("- " + self._warning_text(w))

        if intent == "OPEN_SCENARIO":
            direct = self._answer_open_scenario(context, snapshot)
        elif intent == "EVIDENCE":
            direct = self._answer_evidence(context, focus_plan, question)
        elif intent == "WARNINGS":
            ws = [w for w in context.get("warnings", []) if str(w.get("level", "")).upper() not in {"BLOCKER", "CRITICAL", "HIGH"}]
            direct = [self._warning_text(w) for w in ws] if ws else ["Không có cảnh báo bổ sung ngoài nhóm ưu tiên đã nêu ở trên."]
        elif intent == "HISTORY":
            direct = self._answer_history(context, question)
        elif intent == "FRESHNESS":
            direct = self._answer_freshness(context, question)
        elif intent in {"SCENARIO", "FILL_STATUS"} and self._parse_numbers(question):
            direct = self._answer_scenario(context, focus_plan, question, snapshot)
        elif intent == "FILL_STATUS":
            states = self._state_map(context)
            plans = [focus_plan] if focus_plan else context.get("active_plans", [])
            direct = [self._format_plan(p, states.get(_plan_key(p))) for p in plans if p]
        elif intent == "TARGET":
            direct = self._answer_target(context, focus_plan, memory, question)
        elif intent == "PRIORITY":
            direct = self._answer_priority(context, focus_plan)
        elif intent == "CONSENSUS":
            con = context.get("consensus", {})
            direct = [
                f"Đồng thuận: {con.get('direction') or 'NA'}; strength {con.get('strength', 0):.0%}; "
                f"{con.get('count', 0)} kế hoạch active."
            ] + ["- " + self._format_plan(p) for p in context.get("active_plans", [])]
        elif intent == "WHY" or intent == "COMPARE":
            direct = self._answer_why(context, focus_plan, question)
        elif intent == "CHANGE":
            direct = self._answer_change(context, memory)
        elif intent == "RISK":
            direct = self._answer_priority(context, focus_plan)
            direct.append("Schema không có stop-loss chuẩn; AI không tự bịa stop. Chỉ dùng RiskAction, Units, R5 và cảnh báo catalog.")
        else:
            direct = self._direct_current_plan(context, focus_plan)

        lines.extend(direct)

        if context.get("stale_forward_plans"):
            lines.append("Freshness: có forward basis cũ đã bị loại; chúng không được dùng làm kèo chính.")
        lines.append("Điều chưa biết: thứ tự intraday chỉ được khẳng định khi có bar/event log đủ chi tiết; Độ khớp không phải xác suất thắng.")

        structured = {
            "advisor_version": ADVISOR_VERSION,
            "intent": intent,
            "as_of": context.get("as_of"),
            "focus_plan_key": _plan_key(focus_plan) if focus_plan else None,
            "consensus": context.get("consensus"),
            "warnings": context.get("warnings", []),
            "freshness": context.get("freshness"),
            "answer_sections": {
                "severe_warnings": severe,
                "direct_answer": direct,
                "unknowns": ["Intraday event order may be unknown", "Match score is not probability"],
            },
        }
        return "\n".join(lines), structured

    def ask(
        self,
        question: str,
        *,
        session_id: str = "default",
        as_of: str | None = None,
        live_price: float | None = None,
        session_open: float | None = None,
        session_high: float | None = None,
        session_low: float | None = None,
    ) -> AdvisorReply:
        memory = self.memory_store.load(session_id)
        requested_style, pure_style_command = self._detect_answer_style(question)
        if requested_style:
            memory.answer_style = "focused"
        effective_question = question
        if pure_style_command:
            prior_question = self._last_substantive_question(memory)
            if prior_question:
                effective_question = prior_question

        tabular_ohlc = None if pure_style_command else self._extract_tabular_ohlc(question)
        parsed_date = (tabular_ohlc or {}).get("date") or self._extract_date(effective_question)
        as_of = as_of or parsed_date or memory.focus_date or None

        numbers = self._parse_numbers(effective_question)
        intent_hint = self._classify_intent(effective_question)
        table_has_priority = bool(tabular_ohlc and tabular_ohlc.get("source") == "header_table")
        question_open = (tabular_ohlc or {}).get("open") if table_has_priority else (None if pure_style_command else self._extract_explicit_open(question))
        question_live = (tabular_ohlc or {}).get("close") if table_has_priority else (None if pure_style_command else self._extract_explicit_live_price(question))
        if question_live is None and not table_has_priority and not pure_style_command:
            question_live = self._extract_safe_standalone_live_price(question)
        question_high = (tabular_ohlc or {}).get("high") if table_has_priority else (None if pure_style_command else self._extract_explicit_high(question))
        question_low = (tabular_ohlc or {}).get("low") if table_has_priority else (None if pure_style_command else self._extract_explicit_low(question))
        market_update_applied = bool(tabular_ohlc or any(x is not None for x in (question_open, question_live, question_high, question_low)))
        if tabular_ohlc and not table_has_priority and all(x is None for x in (question_open, question_live, question_high, question_low)):
            question_open = tabular_ohlc.get("open")
            question_live = tabular_ohlc.get("close")
            question_high = tabular_ohlc.get("high")
            question_low = tabular_ohlc.get("low")
        position_side, position_entry = self._extract_position_claim(effective_question)
        hypothetical = self._is_hypothetical_condition(effective_question)
        if position_side:
            memory.tactical_side = position_side
            memory.tactical_mode = "CLAIMED_OPEN_POSITION"
            memory.tactical_entry = position_entry
            memory.position_status = "OPEN"

        # Giá live được nói rõ trong câu hỏi thắng tham số cũ. Giá vào lệnh và
        # các mốc trong câu giả định không được phép masquerade thành tick mới.
        if question_open is not None:
            session_open = question_open
            live_price = question_open if question_live is None else question_live
        elif question_live is not None:
            live_price = question_live
        # No generic numeric fallback: a number inside "chờ/entry/target/khớp 1835"
        # is a plan reference, not a new market tick. Only explicit labels/table/standalone
        # market-price utterances may update live OHLC.

        if session_open is None:
            session_open = memory.last_snapshot.get("session_open")
        if live_price is None:
            live_price = memory.last_live_price
        if question_high is not None:
            session_high = question_high
        elif session_high is None:
            session_high = memory.last_snapshot.get("session_high")
        if question_low is not None:
            session_low = question_low
        elif session_low is None:
            session_low = memory.last_snapshot.get("session_low")

        # Cập nhật range chỉ khi câu hiện tại thực sự cung cấp dữ liệu thị trường mới.
        # Giá kế thừa từ memory dùng để trả lời, nhưng tuyệt đối không được nới High/Low.
        if market_update_applied and question_open is not None:
            session_high = question_open if session_high is None else max(float(session_high), float(question_open))
            session_low = question_open if session_low is None else min(float(session_low), float(question_open))
        if market_update_applied and question_live is not None:
            session_high = float(question_live) if session_high is None else max(float(session_high), float(question_live))
            session_low = float(question_live) if session_low is None else min(float(session_low), float(question_live))

        snapshot = SessionSnapshot(
            as_of=as_of,
            live_price=live_price,
            session_open=session_open,
            session_high=session_high,
            session_low=session_low,
            session_close=(tabular_ohlc or {}).get("close") if tabular_ohlc else None,
            is_completed_bar=bool((tabular_ohlc or {}).get("is_completed_bar")),
            input_source=str((tabular_ohlc or {}).get("source") or ""),
        )
        context = self._build_context(as_of, snapshot)
        intent = self._classify_intent(effective_question)

        resolved_engine = self._resolve_engine(effective_question, memory)
        profile = ""
        engine = resolved_engine
        if resolved_engine.startswith("engine5:"):
            engine, profile = resolved_engine.split(":", 1)
        horizon = self._resolve_horizon(effective_question, memory)
        if engine:
            matches = [p for p in context.get("active_plans", []) if p.get("engine") == engine]
            if profile:
                matches = [p for p in matches if p.get("profile") == profile]
            if horizon:
                matches = [p for p in matches if p.get("horizon") == horizon]
            focus_plan = matches[0] if matches else None
        else:
            focus_plan = self._find_plan(context, engine="", horizon=horizon)

        compose_error = ""
        try:
            text, structured = self._compose(effective_question, intent, context, focus_plan, memory, snapshot)
        except Exception as exc:
            compose_error = f"{type(exc).__name__}: {exc}"
            fallback_lines = self._system_playbook_lines(context, snapshot)
            text = "\n".join(fallback_lines or ["Hệ chưa dựng được câu trả lời, nhưng không có quyền phát lệnh khi chưa đọc được plan trong outputs."])
            structured = {
                "advisor_version": ADVISOR_VERSION,
                "intent": intent,
                "answer_style": "recovery_fallback",
                "internal_error": compose_error,
                "answer_sections": {"direct_answer": fallback_lines},
            }
        if not str(text or "").strip():
            fallback_lines = self._system_playbook_lines(context, snapshot)
            text = "\n".join(fallback_lines or ["Không có câu trả lời hợp lệ từ planner; tạm thời NO TRADE và kiểm tra lại outputs."])
            structured = dict(structured or {})
            structured["empty_answer_recovered"] = True
            structured.setdefault("answer_sections", {})["direct_answer"] = fallback_lines
        frame = structured.get("reasoning_frame") or {}
        if frame.get("tactical_side"):
            # Chỉ quyết định đã được planner chấp nhận mới thay đổi trạng thái chiến thuật.
            # Một câu hỏi bị R5 KEEP/CANCEL/FLIP_HINT chặn không được ghi đè vị thế đang quản lý.
            memory.tactical_side = str(frame.get("tactical_side"))
            memory.tactical_mode = str(frame.get("tactical_mode") or "")
            if memory.tactical_side == "FLAT":
                memory.position_status = "CLOSED"
            elif memory.tactical_side in {"LONG", "SHORT"}:
                memory.position_status = "OPEN" if memory.tactical_mode in {"aligned", "countertrend", "CLAIMED_OPEN_POSITION"} else memory.position_status
                if memory.position_status == "OPEN" and live_price is not None:
                    memory.tactical_entry = float(live_price)

        if focus_plan:
            memory.focus_engine = focus_plan.get("engine", "")
            memory.focus_horizon = focus_plan.get("horizon", "")
            memory.focus_date = focus_plan.get("date", "")
        elif context.get("as_of"):
            memory.focus_date = context.get("as_of", "")
        memory.last_live_price = live_price
        if market_update_applied:
            memory.last_snapshot_source = str((tabular_ohlc or {}).get("source") or "explicit_labeled_price")
            memory.last_market_update_question = question
        memory.last_snapshot = {
            "session_open": session_open,
            "session_high": session_high,
            "session_low": session_low,
        }
        previous_fp = memory.last_context_fingerprint
        current_fp = self._fingerprint(context)
        memory.turns.append(Turn(
            question=question,
            answer=text,
            intent=intent,
            as_of=context.get("as_of", ""),
            focus_engine=memory.focus_engine,
            focus_horizon=memory.focus_horizon,
        ))
        memory.trim(int(self.policy.get("memory", {}).get("max_turns", 30)))
        memory.last_context_fingerprint = current_fp
        self.memory_store.save(memory)
        structured["context_changed_since_previous_turn"] = bool(previous_fp and previous_fp != current_fp)
        structured["market_input"] = {
            "update_applied": market_update_applied,
            "source": str((tabular_ohlc or {}).get("source") or ("explicit_labeled_price" if market_update_applied else "conversation_reference_only")),
            "trusted_snapshot": memory.last_snapshot,
        }
        structured["memory"] = {
            "focus_engine": memory.focus_engine,
            "focus_horizon": memory.focus_horizon,
            "focus_date": memory.focus_date,
            "turn_count": len(memory.turns),
            "answer_style": memory.answer_style,
            "last_live_price": memory.last_live_price,
            "tactical_side": memory.tactical_side,
            "tactical_mode": memory.tactical_mode,
            "tactical_entry": memory.tactical_entry,
            "position_status": memory.position_status,
            "last_snapshot_source": memory.last_snapshot_source,
            "market_update_applied_this_turn": market_update_applied,
        }
        return AdvisorReply(text=text, intent=intent, context=context, focus_plan=focus_plan, structured=structured)

    def make_llm_payload(self, question: str, **kwargs: Any) -> dict[str, Any]:
        reply = self.ask(question, **kwargs)
        payload = llm_payload(question, reply.context)
        payload["smart_advisor"] = reply.structured
        payload["conversation_policy"] = self.policy
        return payload
