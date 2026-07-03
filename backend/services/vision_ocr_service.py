"""Vision LLM OCR Service — replaces TrOCR pipeline.

Sends answer sheet images directly to a vision LLM via OpenRouter.
Returns structured answers per question number.

Supported models via OpenRouter:
  - google/gemini-2.0-flash-001      (fast, cheap, good vision)
  - anthropic/claude-3.5-sonnet      (best accuracy)
  - openai/gpt-4o                    (strong vision)
  - google/gemini-flash-1.5          (cheapest)
"""

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI


def _encode_image(image_path: str) -> str:
    """Encode image to base64."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _build_extraction_prompt(questions: List[Dict]) -> str:
    """Build prompt asking LLM to extract answers per question."""
    q_lines = []
    for q in questions:
        line = f"Q{q['number']} [{q.get('section', '')} — {q.get('maxMarks', 1)} mark(s)]: {q['text']}"
        opts = q.get("options") or []
        if len(opts) >= 2:
            labels = ["A", "B", "C", "D"]
            opts_str = "  ".join(f"{labels[i]}) {opts[i]}" for i in range(min(len(opts), 4)))
            line += f"\n   Options: {opts_str}"
        q_lines.append(line)

    questions_text = "\n".join(q_lines)

    return f"""You are grading a Class 8 Biology answer sheet written by a student in India.

The question paper has these questions:
{questions_text}

Look at the handwritten answer sheet image carefully and extract what the student has written for each question.

Return ONLY a valid JSON array, no markdown, no explanation. Format:
[
  {{
    "questionNumber": 1,
    "mcqChoice": "A",
    "extractedAnswer": "the student's written answer or chosen option",
    "isEmpty": false,
    "isDiagram": false
  }}
]

Rules:
- For MCQ (Q1-Q10): set mcqChoice to "A"/"B"/"C"/"D" if you can identify it, else null
- For subjective (Q11+): set mcqChoice to null, put full written text in extractedAnswer
- If student left it blank: set isEmpty to true, extractedAnswer to ""
- If student drew a diagram: set isDiagram to true
- Include ALL {len(questions)} questions in the array even if blank
- Do not guess or hallucinate — only write what is clearly visible"""


def _build_grading_prompt(items: List[Dict]) -> str:
    """Build prompt for batch semantic grading of subjective answers."""
    lines = []
    for item in items:
        lines.append(
            f"Q{item['qNum']} [{item['maxMarks']} mark(s)]\n"
            f"  Question: {item['questionText']}\n"
            f"  Expected answer: {item['expected']}\n"
            f"  Student wrote: {item['studentAnswer']}"
        )

    questions_block = "\n\n".join(lines)

    return f"""You are a Class 8 Biology teacher in India grading student answer sheets.

Grade the following subjective answers. Award marks based on semantic correctness — synonyms, equivalent phrases, and correct concepts written differently should receive full credit.

{questions_block}

Return ONLY a valid JSON array, one entry per question, no markdown:
[
  {{
    "questionNumber": 11,
    "marksAwarded": 2.0,
    "maxMarks": 2,
    "confidenceScore": 85,
    "reasoning": "Student correctly identified X and Y. Missed Z."
  }}
]

Rules:
- marksAwarded must be between 0 and maxMarks (can be partial e.g. 1.0 out of 2)
- confidenceScore: 80-100 = high confidence, 50-79 = medium, below 50 = low
- reasoning: brief 1-2 sentence explanation in teacher's voice
- Award full marks for semantically correct answers even if worded differently
- Do not penalise for spelling mistakes if the meaning is clear"""


def _call_llm(client: OpenAI, model: str, messages: list, max_tokens: int = 8000) -> str:
    """Call OpenRouter LLM and return response text."""
    extra = {}
    if model.startswith("~"):
        extra["reasoning"] = {"enabled": True}

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.1,
        max_tokens=max_tokens,
        extra_body=extra,
        extra_headers={"X-No-Cache": "true"},
    )
    return response.choices[0].message.content.strip()


def extract_answers_from_image(
    image_paths: List[str],
    questions: List[Dict],
    api_key: str,
    model: str = "google/gemini-2.0-flash-001",
) -> List[Dict[str, Any]]:
    """
    Send answer sheet image(s) to vision LLM and extract structured answers.

    Args:
        image_paths: List of image file paths (one per page)
        questions: List of question dicts with number, text, options, maxMarks
        api_key: OpenRouter API key
        model: OpenRouter model ID

    Returns:
        List of {questionNumber, mcqChoice, extractedAnswer, isEmpty, isDiagram}
    """
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set")

    # Build message content with all images + prompt
    content = []

    for path in image_paths:
        ext = Path(path).suffix.lower().lstrip(".")
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        b64 = _encode_image(path)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"}
        })

    content.append({
        "type": "text",
        "text": _build_extraction_prompt(questions)
    })

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    print(f"  [Step 1/2] Extracting answers via {model}...")
    response_text = _call_llm(client, model, [{"role": "user", "content": content}])

    # Extract JSON from response
    json_start = response_text.find("[")
    json_end = response_text.rfind("]")
    if json_start == -1 or json_end == -1:
        raise ValueError(f"No JSON array in response: {response_text[:200]}")

    structured = json.loads(response_text[json_start:json_end + 1])

    # Validate — ensure all questions present
    found_nums = {e.get("questionNumber") for e in structured}
    for q in questions:
        if q["number"] not in found_nums:
            structured.append({
                "questionNumber": q["number"],
                "mcqChoice": None,
                "extractedAnswer": "",
                "isEmpty": True,
                "isDiagram": False,
            })

    structured.sort(key=lambda x: x.get("questionNumber", 999))
    return structured


def grade_answers(
    structured: List[Dict],
    questions: List[Dict],
    answer_key: Optional[List[Dict]] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Grade extracted answers against answer key or question expected answers.
    Uses LLM for semantic grading of subjective questions when api_key is provided.

    Args:
        structured: Output from extract_answers_from_image
        questions: Question definitions
        answer_key: Optional parsed answer key [{questionNumber, correctOption, expectedText}]
        api_key: OpenRouter API key (enables LLM grading for subjective)
        model: OpenRouter model ID

    Returns:
        List of evaluation dicts ready for MongoDB
    """
    q_by_num = {q["number"]: q for q in questions}
    key_by_num = {k["questionNumber"]: k for k in (answer_key or [])}

    evaluations = []
    subjective_pending = []  # collect subjective answers for batch LLM grading

    for entry in structured:
        q_num = entry.get("questionNumber")
        q = q_by_num.get(q_num)
        if not q:
            continue

        ev = {
            "qId": f"q{q_num}",
            "studentAnswer": entry.get("extractedAnswer", ""),
            "mcqChoice": entry.get("mcqChoice"),
            "isEmpty": entry.get("isEmpty", False),
            "isDiagram": entry.get("isDiagram", False),
            "aiMark": 0.0,
            "maxMarks": q.get("maxMarks", 1),
            "confidence": "high",
            "confidenceScore": 90,
            "needsReview": False,
            "reasoning": "",
        }

        if entry.get("isEmpty"):
            ev["aiMark"] = 0.0
            ev["reasoning"] = "No answer written."
            ev["needsReview"] = False
            evaluations.append(ev)
            continue

        if entry.get("isDiagram"):
            ev["aiMark"] = 0.0
            ev["confidence"] = "low"
            ev["needsReview"] = True
            ev["reasoning"] = "Diagram detected — manual review needed."
            evaluations.append(ev)
            continue

        key = key_by_num.get(q_num)
        is_mcq = "options" in q

        if is_mcq:
            ev["aiMark"], ev["confidenceScore"], ev["reasoning"] = _grade_mcq(entry, q, key)
            ev["confidence"] = "high" if ev["confidenceScore"] >= 80 else "medium" if ev["confidenceScore"] >= 50 else "low"
            ev["needsReview"] = ev["confidence"] in ("low", "medium")
            evaluations.append(ev)
        else:
            # Queue for LLM batch grading; add placeholder for now
            ev["aiMark"] = 0.0
            ev["confidenceScore"] = 50
            ev["reasoning"] = "Pending LLM grading."
            ev["needsReview"] = True
            evaluations.append(ev)

            expected = ""
            if key and key.get("expectedText"):
                expected = key["expectedText"]
            elif q.get("expected"):
                expected = q["expected"]

            if expected and entry.get("extractedAnswer", "").strip():
                subjective_pending.append({
                    "qNum": q_num,
                    "maxMarks": q.get("maxMarks", 1),
                    "questionText": q.get("text", ""),
                    "expected": expected,
                    "studentAnswer": entry.get("extractedAnswer", ""),
                    "ev_index": len(evaluations) - 1,
                })

    # Batch LLM grading for subjective answers — use vision model with thinking
    if subjective_pending and api_key and model:
        print(f"  [Step 2/2] Semantic grading of {len(subjective_pending)} subjective answer(s) via {model}...")
        llm_results = _grade_subjective_batch(subjective_pending, api_key, model)
        for result in llm_results:
            idx = result.get("ev_index")
            if idx is None:
                continue
            ev = evaluations[idx]
            ev["aiMark"] = float(result.get("marksAwarded", 0.0))
            ev["confidenceScore"] = int(result.get("confidenceScore", 50))
            ev["reasoning"] = result.get("reasoning", "")
            ev["confidence"] = "high" if ev["confidenceScore"] >= 80 else "medium" if ev["confidenceScore"] >= 50 else "low"
            ev["needsReview"] = ev["confidence"] in ("low", "medium")
    elif subjective_pending:
        # Fallback to keyword matching if no LLM available
        for item in subjective_pending:
            ev = evaluations[item["ev_index"]]
            entry = next((e for e in structured if e.get("questionNumber") == item["qNum"]), {})
            q = q_by_num.get(item["qNum"])
            key = key_by_num.get(item["qNum"])
            ev["aiMark"], ev["confidenceScore"], ev["reasoning"] = _grade_subjective_keywords(entry, q, key)
            ev["confidence"] = "high" if ev["confidenceScore"] >= 80 else "medium" if ev["confidenceScore"] >= 50 else "low"
            ev["needsReview"] = ev["confidence"] in ("low", "medium")

    return evaluations


def _grade_subjective_batch(
    items: List[Dict],
    api_key: str,
    model: str,
) -> List[Dict]:
    """Send all subjective answers to LLM in one call for semantic grading."""
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    prompt = _build_grading_prompt(items)
    response_text = _call_llm(client, model, [{"role": "user", "content": prompt}], max_tokens=4000)

    json_start = response_text.find("[")
    json_end = response_text.rfind("]")
    if json_start == -1 or json_end == -1:
        print(f"  Warning: LLM grading returned no JSON, falling back to keyword match")
        return []

    llm_grades = json.loads(response_text[json_start:json_end + 1])

    # Map back to ev_index via questionNumber
    q_to_idx = {item["qNum"]: item["ev_index"] for item in items}
    results = []
    for grade in llm_grades:
        q_num = grade.get("questionNumber")
        if q_num in q_to_idx:
            grade["ev_index"] = q_to_idx[q_num]
            results.append(grade)

    return results


def _grade_mcq(entry: Dict, q: Dict, key: Optional[Dict]) -> tuple:
    student_choice = entry.get("mcqChoice")
    if not student_choice:
        return 0.0, 40, "Could not determine MCQ choice."

    correct_letter = None
    if key and key.get("correctOption"):
        correct_letter = key["correctOption"].upper()
    elif q.get("correctAnswer"):
        for i, opt in enumerate(q.get("options", [])):
            if opt == q["correctAnswer"]:
                correct_letter = chr(ord("A") + i)
                break

    if not correct_letter:
        return 0.0, 30, "No answer key provided for this question."

    if student_choice.upper() == correct_letter:
        return float(q.get("maxMarks", 1)), 95, f"Correct — option {student_choice}."
    return 0.0, 95, f"Wrong — student chose {student_choice}, correct is {correct_letter}."


def _grade_subjective_keywords(entry: Dict, q: Dict, key: Optional[Dict]) -> tuple:
    """Fallback keyword-overlap grading (used when LLM unavailable)."""
    extracted = (entry.get("extractedAnswer") or "").strip().lower()
    if not extracted:
        return 0.0, 20, "No answer extracted."

    expected = ""
    if key and key.get("expectedText"):
        expected = key["expectedText"].lower()
    elif q.get("expected"):
        expected = q["expected"].lower()

    if not expected:
        return float(q.get("maxMarks", 1)) * 0.5, 40, "No expected answer — manual review needed."

    expected_words = set(expected.split())
    student_words = set(extracted.split())
    if not expected_words:
        return 0.0, 30, "Empty expected answer."

    overlap = len(expected_words & student_words) / len(expected_words)
    max_marks = q.get("maxMarks", 1)

    if overlap >= 0.7:
        return float(max_marks), 85, f"Strong match ({int(overlap*100)}% keywords)."
    elif overlap >= 0.4:
        return round(max_marks * 0.5, 1), 60, f"Partial match ({int(overlap*100)}% keywords)."
    elif overlap > 0:
        return round(max_marks * 0.25, 1), 35, "Minimal keyword overlap."
    return 0.0, 20, "No matching keywords."
