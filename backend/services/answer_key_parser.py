"""Parse teacher-submitted answer key and questions text into structured JSON.

Uses heuristic parsing first (fast, offline).
Falls back to OpenRouter LLM for complex/ambiguous formats.
"""

import json
import re
from typing import List, Dict, Optional, Any

from backend.core.config import settings


def _call_openrouter(prompt: str, max_tokens: int = 4000) -> str:
    """Call OpenRouter with the text model (no vision needed)."""
    from openai import OpenAI
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=settings.OPENROUTER_API_KEY)
    model = settings.TEXT_MODEL.lstrip("~")
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=max_tokens,
        extra_headers={"X-No-Cache": "true"},
    )
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Answer Key Parsing
# ---------------------------------------------------------------------------

def parse_answer_key(text: str) -> List[Dict[str, Any]]:
    """Parse free-text answer key into structured array.

    Returns list of {questionNumber, correctAnswer, correctOption, expectedText}.
    """
    heuristic = _heuristic_parse_key(text)
    if heuristic and len(heuristic) >= 3:
        return heuristic

    # Fall back to LLM
    try:
        return _llm_parse_answer_key(text) or heuristic or []
    except Exception as e:
        print(f"  LLM answer key parsing failed: {e}")
        return heuristic or []


def _heuristic_parse_key(text: str) -> List[Dict[str, Any]]:
    """Fast rule-based extraction of MCQ and short-answer keys."""
    results = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    for line in lines:
        # MCQ: "1. A" or "1) B" or "Q1. A"
        mcq = re.match(r'^Q?\s*(\d{1,2})\s*[.)\s]\s*([A-Da-d])\b', line)
        if mcq:
            q_num = int(mcq.group(1))
            letter = mcq.group(2).upper()
            rest = line[mcq.end():].strip()
            answer_text = rest if rest and len(rest) > 1 else f"Option {letter}"
            results.append({
                "questionNumber": q_num,
                "correctOption": letter,
                "correctAnswer": answer_text,
                "expectedText": answer_text,
            })
            continue

        # Subjective: "11. Weeds are unwanted plants..."
        subj = re.match(r'^Q?\s*(\d{1,2})\s*[.)]\s+(.+)$', line)
        if subj:
            q_num = int(subj.group(1))
            answer_text = subj.group(2).strip()
            results.append({
                "questionNumber": q_num,
                "correctOption": None,
                "correctAnswer": answer_text,
                "expectedText": answer_text,
            })

    if len(results) >= 3:
        results.sort(key=lambda r: r["questionNumber"])
        return results
    return []


def _llm_parse_answer_key(text: str) -> List[Dict[str, Any]]:
    prompt = f"""Parse the following answer key into a JSON array. Each item must have:
- questionNumber: int
- correctOption: "A"/"B"/"C"/"D" or null (only for MCQs)
- correctAnswer: the correct answer text
- expectedText: what a good student answer should contain

ANSWER KEY:
{text[:5000]}

Return ONLY a valid JSON array. No markdown, no explanation."""

    response_text = _call_openrouter(prompt, max_tokens=3000)
    j0 = response_text.find("[")
    j1 = response_text.rfind("]")
    if j0 >= 0 and j1 > j0:
        parsed = json.loads(response_text[j0:j1 + 1])
        if isinstance(parsed, list) and len(parsed) >= 1:
            return parsed
    return []


# ---------------------------------------------------------------------------
# Questions Text Parsing
# ---------------------------------------------------------------------------

def parse_questions_text(text: str) -> List[Dict[str, Any]]:
    """Parse teacher-submitted questions text into structured question array."""
    heuristic = _heuristic_parse_questions(text)

    # Only trust heuristic if it got reasonable results
    # Sanity check: unique question numbers, reasonable count
    if heuristic:
        nums = [q["number"] for q in heuristic]
        unique = len(set(nums)) == len(nums)
        reasonable = 5 <= len(heuristic) <= 30
        if unique and reasonable:
            print(f"  Heuristically parsed {len(heuristic)} questions.")
            return heuristic

    # Fall back to LLM
    print(f"  Heuristic parse gave {len(heuristic)} questions — falling back to LLM...")
    try:
        llm_result = _llm_parse_questions(text)
        if llm_result:
            return llm_result
    except Exception as e:
        print(f"  LLM questions parsing failed: {e}")

    return heuristic or []


def _llm_parse_questions(text: str) -> List[Dict[str, Any]]:
    prompt = f"""Parse the following exam question paper into a JSON array.

Each question must have:
- number: int (the question number, e.g. 1, 2, ... 17)
- text: string (the question text only, no options)
- section: "A", "B", "C", or "D"
- maxMarks: int (1 for section A, 2 for B, 4 for C, 8 for D)
- options: array of 4 strings if MCQ (just the option text, no A/B/C/D prefix), else empty array []
- correctAnswer: null (leave blank, answer key is separate)
- expected: null

Important rules:
- Count each numbered question ONCE. Sub-items (i, ii, iii, iv) are part of one question, not separate questions.
- Header lines like "SECTION A", "Total Marks: 40" are NOT questions.
- Option lines like "A) Frog  B) Butterfly" belong to the question above them.
- The paper has exactly the questions that are numbered (1, 2, 3... up to the last number you see).

QUESTION PAPER:
{text[:8000]}

Return ONLY a valid JSON array. No markdown, no explanation."""

    response_text = _call_openrouter(prompt, max_tokens=4000)
    j0 = response_text.find("[")
    j1 = response_text.rfind("]")
    if j0 >= 0 and j1 > j0:
        parsed = json.loads(response_text[j0:j1 + 1])
        if isinstance(parsed, list) and len(parsed) >= 1:
            for i, q in enumerate(parsed):
                q["id"] = f"q{q.get('number', i + 1)}"
                q["_id"] = q["id"]
                q["assessmentId"] = "__parsed__"
                if "options" not in q:
                    q["options"] = []
                if "correctAnswer" not in q:
                    q["correctAnswer"] = None
                if "expected" not in q:
                    q["expected"] = None
            return parsed
    return []


def _heuristic_parse_questions(text: str) -> List[Dict[str, Any]]:
    """Heuristically parse free-text questions — conservative, only numbered lines."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    questions = []
    current_q = None

    SECTION_MARKS = {"A": 1, "B": 2, "C": 4, "D": 8}
    current_section = "A"

    for line in lines:
        lower = line.lower()

        # Detect section headers
        if re.match(r'^section\s+[a-d]\b', lower):
            m = re.search(r'section\s+([a-d])', lower)
            if m:
                current_section = m.group(1).upper()
            continue

        # Skip pure header/meta lines (no leading number)
        if not re.match(r'^[Qq]?\s*\d', line):
            # Could be option line for current question
            opts = re.findall(r'[A-D]\)\s*([^A-D\n]+?)(?=\s+[A-D]\)|$)', line)
            if opts and current_q is not None and not current_q.get("options"):
                current_q["options"] = [o.strip() for o in opts]
            continue

        # Numbered question line
        m = re.match(r'^[Qq]?\s*(\d{1,2})\s*[.)]\s*(.+)$', line)
        if not m:
            continue

        q_num = int(m.group(1))
        q_text = m.group(2).strip()

        # Determine section from number if not set by header
        if q_num <= 10:
            sect = "A"
        elif q_num <= 13:
            sect = "B"
        elif q_num <= 15:
            sect = "C"
        else:
            sect = "D"

        # Check for inline options: "1. Question text A) opt B) opt..."
        inline_opts = re.findall(r'([A-D])\)\s*([^A-D\n]+?)(?=\s+[A-D]\)|$)', q_text)
        options = []
        if inline_opts:
            options = [o[1].strip() for o in inline_opts]
            q_text = re.split(r'\s+[A-D]\)', q_text)[0].strip()

        current_q = {
            "id": f"q{q_num}",
            "_id": f"q{q_num}",
            "number": q_num,
            "section": sect,
            "maxMarks": SECTION_MARKS.get(sect, 1),
            "text": q_text,
            "options": options,
            "correctAnswer": None,
            "expected": None,
            "assessmentId": "__parsed__",
        }
        questions.append(current_q)

    return questions


# ---------------------------------------------------------------------------
# Curriculum Parsing (heuristic only — low stakes)
# ---------------------------------------------------------------------------

def parse_curriculum_text(text: str) -> List[Dict[str, Any]]:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    chapters = []
    current_chapter = None
    ch_counter = 0
    colors = ["blue", "emerald", "amber", "rose", "violet", "orange", "teal"]

    for line in lines:
        ch_match = re.match(r'^(?:Chapter|Ch|Lesson)\s*(\d+)[:\-.\s)]+\s*(.+)$', line, re.IGNORECASE)
        if not ch_match:
            ch_match = re.match(r'^(\d+)\.\s*(.+?)(?:\s*[-–—]\s*.+)?$', line)

        if ch_match:
            ch_counter += 1
            current_chapter = {
                "id": f"ch{ch_counter}",
                "name": ch_match.group(2).strip()[:80],
                "order": ch_counter,
                "color": colors[(ch_counter - 1) % len(colors)],
                "concepts": [],
            }
            chapters.append(current_chapter)
            continue

        concept_match = re.match(r'^[-*•]\s*(.+)$', line)
        if not concept_match:
            concept_match = re.match(r'^(?:Topic|Concept|Sub-topic)[:\s]+(.+)$', line, re.IGNORECASE)

        if concept_match and current_chapter is not None:
            concept_text = concept_match.group(1).strip()
            parts = [p.strip() for p in concept_text.split(",")]
            current_chapter["concepts"].append({
                "name": parts[0][:60],
                "keywords": parts[1:4],
                "description": concept_text[:200],
                "prerequisites": [],
                "difficulty": "Medium",
                "expectedSkills": ["Recall"],
            })

    return chapters
