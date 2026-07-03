from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, BackgroundTasks
from typing import List, Optional
from backend.core.database import get_db
from backend.core.config import settings
from backend.models.assessment import Assessment, AssessmentCreate, AssessmentProcessRequest
from datetime import datetime, timezone
import uuid
import os
import json
import bcrypt
import shutil
import asyncio

router = APIRouter()

UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "media", "uploads")


def _save_uploaded_files(assessment_id: str, files: List[UploadFile], subdir: str) -> List[str]:
    """Save uploaded files to media/uploads/{assessment_id}/{subdir}/. Returns list of saved paths."""
    if not files:
        return []
    target = os.path.join(UPLOADS_DIR, assessment_id, subdir)
    os.makedirs(target, exist_ok=True)
    saved = []
    for f in files:
        if not f.filename:
            continue
        safe_name = f.filename.replace(" ", "_").replace("/", "_")
        dest = os.path.join(target, safe_name)
        with open(dest, "wb") as buf:
            shutil.copyfileobj(f.file, buf)
        # Store relative to media for frontend access: media/uploads/{aid}/{subdir}/{file}
        saved.append(f"media/uploads/{assessment_id}/{subdir}/{safe_name}")
    return saved


@router.get("", response_model=List[Assessment])
async def get_assessments(db=Depends(get_db)):
    assessments = await db.assessments.find().to_list(100)
    return assessments


@router.get("/seed")
async def seed_database(db=Depends(get_db)):
    """Seed the database with demo data. Call this once after deployment."""
    seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed")

    await db.curricula.delete_many({})
    await db.assessments.delete_many({})
    await db.questions.delete_many({})
    await db.students.delete_many({})
    await db.evaluations.delete_many({})
    await db.interventions.delete_many({})
    await db.users.delete_many({})

    hashed = bcrypt.hashpw("demo1234".encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    await db.users.insert_one({
        "_id": "teacher-1",
        "name": "Lakshmi Devi",
        "email": "teacher@school.gov.in",
        "school": "Z.P. High School, Hyderabad",
        "subjects": ["Biology", "Physics"],
        "password_hash": hashed,
    })

    curr_path = os.path.join(seed_dir, "curriculum", "ap-class8-bio.json")
    if os.path.exists(curr_path):
        with open(curr_path) as f:
            await db.curricula.insert_one(json.load(f))

    data_dir = os.path.join(seed_dir, "data")
    for filename, collection_name in [
        ("assessments.json", "assessments"),
        ("questions.json", "questions"),
        ("students.json", "students"),
        ("evaluations.json", "evaluations"),
        ("interventions.json", "interventions"),
    ]:
        filepath = os.path.join(data_dir, filename)
        if not os.path.exists(filepath):
            continue
        with open(filepath) as f:
            docs = json.load(f)
        if isinstance(docs, dict):
            items = []
            for student_id, student_evals in docs.items():
                for e in student_evals:
                    e["_id"] = f"{student_id}-{e['qId']}"
                    e["assessmentId"] = "asm-001"
                    e["studentId"] = student_id
                    e["approved"] = False
                    items.append(e)
            if items:
                await db[collection_name].insert_many(items)
        elif isinstance(docs, list):
            for d in docs:
                if "id" in d:
                    d["_id"] = d.pop("id")
                if collection_name != "assessments":
                    d["assessmentId"] = "asm-001"
            if docs:
                await db[collection_name].insert_many(docs)

    return {
        "status": "ok",
        "message": "Database seeded successfully. Login: teacher@school.gov.in / demo1234",
    }


@router.get("/{id}", response_model=Assessment)
async def get_assessment(id: str, db=Depends(get_db)):
    assessment = await db.assessments.find_one({"_id": id})
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return assessment


@router.post("", response_model=Assessment)
async def create_assessment(
    db=Depends(get_db),
    name: str = Form(...),
    class_name: str = Form(..., alias="class"),
    subject: str = Form(...),
    type: str = Form(...),
    totalMarks: int = Form(...),
    questionsText: Optional[str] = Form(None),
    answerKeyText: Optional[str] = Form(None),
    curriculumText: Optional[str] = Form(None),
    questionFiles: List[UploadFile] = File(default=[]),
    answerKeyFiles: List[UploadFile] = File(default=[]),
    sheetFiles: List[UploadFile] = File(default=[]),
):
    """Create a new assessment with optional files and text content."""
    assessment_id = f"asm-{uuid.uuid4().hex[:6]}"
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Save uploaded files
    question_images = _save_uploaded_files(
        assessment_id, questionFiles or [], "questions"
    )
    answer_key_images = _save_uploaded_files(
        assessment_id, answerKeyFiles or [], "answer_key"
    )
    sheet_images = _save_uploaded_files(
        assessment_id, sheetFiles or [], "sheets"
    )

    doc = {
        "_id": assessment_id,
        "name": name,
        "class": class_name,
        "subject": subject,
        "type": type,
        "totalMarks": totalMarks,
        "totalPapers": len(sheet_images),
        "pendingReview": len(sheet_images),
        "avgScore": 0.0,
        "status": "draft",
        "createdAt": created_at,
        "questionsText": questionsText,
        "answerKeyText": answerKeyText,
        "curriculumText": curriculumText,
        "questionsImages": question_images,
        "answerKeyImages": answer_key_images,
        "sheetImages": sheet_images,
        "processingStatus": "pending",
        "parsedQuestions": None,
        "parsedAnswerKey": None,
    }

    await db.assessments.insert_one(doc)

    # If answer key text was provided, parse it now
    if answerKeyText and answerKeyText.strip():
        try:
            from backend.services.answer_key_parser import parse_answer_key
            parsed = parse_answer_key(answerKeyText)
            if parsed:
                await db.assessments.update_one(
                    {"_id": assessment_id},
                    {"$set": {"parsedAnswerKey": parsed}}
                )
                doc["parsedAnswerKey"] = parsed
        except Exception as e:
            print(f"  Answer key parsing skipped (Ollama may not be running): {e}")

    # If questions text was provided, parse it now
    if questionsText and questionsText.strip():
        try:
            from backend.services.answer_key_parser import parse_questions_text
            parsed_qs = parse_questions_text(questionsText)
            if parsed_qs:
                await db.assessments.update_one(
                    {"_id": assessment_id},
                    {"$set": {"parsedQuestions": parsed_qs}}
                )
                doc["parsedQuestions"] = parsed_qs
        except Exception as e:
            print(f"  Questions parsing skipped (Ollama may not be running): {e}")

    # If curriculum text was provided, parse it now
    if curriculumText and curriculumText.strip():
        try:
            from backend.services.answer_key_parser import parse_curriculum_text
            parsed_curr = parse_curriculum_text(curriculumText)
            if parsed_curr:
                await db.assessments.update_one(
                    {"_id": assessment_id},
                    {"$set": {"parsedCurriculum": parsed_curr}}
                )
                doc["parsedCurriculum"] = parsed_curr
        except Exception as e:
            print(f"  Curriculum parsing skipped: {e}")

    return doc


@router.post("/{id}/process")
async def process_assessment(
    id: str,
    background_tasks: BackgroundTasks,
    db=Depends(get_db),
):
    """Trigger OCR pipeline processing for the assessment."""
    assessment = await db.assessments.find_one({"_id": id})
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found")

    if not assessment.get("sheetImages"):
        raise HTTPException(status_code=400, detail="No student answer sheets uploaded")

    await db.assessments.update_one(
        {"_id": id},
        {"$set": {"status": "processing", "processingStatus": "ocr"}}
    )

    background_tasks.add_task(_run_ocr_pipeline, id, assessment)
    return {"status": "processing", "assessmentId": id}


@router.post("/{id}/append-sheets", response_model=Assessment)
async def append_sheets(
    id: str,
    background_tasks: BackgroundTasks,
    db=Depends(get_db),
    sheetFiles: List[UploadFile] = File(default=[]),
):
    """Append new student answer sheets to an existing assessment and trigger OCR."""
    assessment = await db.assessments.find_one({"_id": id})
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found")

    if not sheetFiles:
        raise HTTPException(status_code=400, detail="No new student answer sheets uploaded")

    # Save new files
    new_sheet_images = _save_uploaded_files(
        id, sheetFiles, "sheets"
    )

    # Append to existing sheet images
    existing_sheets = assessment.get("sheetImages", []) or []
    updated_sheets = existing_sheets + new_sheet_images

    # Increment total papers
    new_total_papers = len(updated_sheets)

    await db.assessments.update_one(
        {"_id": id},
        {
            "$set": {
                "sheetImages": updated_sheets,
                "totalPapers": new_total_papers,
                "status": "processing",
                "processingStatus": "step_ocr",
            }
        }
    )

    # Run OCR background task ONLY on the newly uploaded sheets!
    # Convert relative media paths to absolute file system paths
    abs_new_paths = [
        os.path.join(os.path.dirname(__file__), "..", "..", img)
        for img in new_sheet_images
    ]

    background_tasks.add_task(_run_ocr_pipeline, id, assessment, abs_new_paths)
    
    # Return updated assessment
    updated_doc = await db.assessments.find_one({"_id": id})
    return updated_doc


async def _run_ocr_pipeline(
    assessment_id: str,
    assessment: dict,
    custom_sheet_paths: Optional[List[str]] = None,
):
    """Background task: run OCR on student sheets, grade against answer key."""
    import sys
    from pathlib import Path
    # Add project root and backend to path
    project_root = str(Path(__file__).resolve().parents[2])
    backend_dir = str(Path(__file__).resolve().parents[1])
    for p in [project_root, backend_dir]:
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        from backend.core.database import get_db as _get_db
        from backend.services.vision_ocr_service import extract_answers_from_image, grade_answers

        db = _get_db()

        # Build the list of answer sheet image paths
        sheet_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "media", "uploads", assessment_id, "sheets"
        )
        sheet_paths = [
            os.path.join(sheet_dir, f)
            for f in os.listdir(sheet_dir)
            if f.lower().endswith((".jpg", ".jpeg", ".png"))
        ] if os.path.exists(sheet_dir) else []

        if not sheet_paths:
            sheet_paths = [
                os.path.join(os.path.dirname(__file__), "..", "..", img)
                for img in assessment.get("sheetImages", [])
            ]

        if not sheet_paths:
            await db.assessments.update_one(
                {"_id": assessment_id},
                {"$set": {"status": "error", "processingStatus": "no_sheets_found"}}
            )
            return

        parsed_key = assessment.get("parsedAnswerKey")
        parsed_questions = assessment.get("parsedQuestions")

        # Load questions — use parsed ones or fall back to seed questions
        if parsed_questions:
            questions = parsed_questions
            for q in questions:
                q["assessmentId"] = assessment_id
                q["_id"] = q.get("_id", f"q-{assessment_id}-{q.get('number', 0)}")
                await db.questions.update_one({"_id": q["_id"]}, {"$set": q}, upsert=True)
        else:
            questions_path = str(
                Path(__file__).resolve().parents[2]
                / "backend" / "seed" / "data" / "questions.json"
            )
            with open(questions_path) as f:
                questions = json.load(f)

        api_key = settings.OPENROUTER_API_KEY
        model = settings.VISION_MODEL

        # [Step 1/6] Scanning handwriting via Vision LLM
        await db.assessments.update_one(
            {"_id": assessment_id},
            {"$set": {"processingStatus": "step_ocr"}}
        )

        # Group sheet paths by student name (from filename)
        student_groups = {}
        for path in sheet_paths:
            base_fname = os.path.basename(path)
            name_part = os.path.splitext(base_fname)[0]
            if "_" in name_part:
                name_part = name_part.split("_")[0]
            student_name = name_part.capitalize()
            if student_name not in student_groups:
                student_groups[student_name] = []
            student_groups[student_name].append(path)

        # Process each student via Vision LLM
        all_student_evaluations = []
        for student_name, student_paths in student_groups.items():
            student_id = f"stu-{assessment_id}-{student_name.lower()}"
            print(f"\n  Processing {student_name} ({len(student_paths)} page(s)) via {model}...")

            structured = await asyncio.to_thread(
                extract_answers_from_image,
                student_paths,
                questions,
                api_key,
                model,
            )

            evaluations = grade_answers(structured, questions, parsed_key, api_key, model)

            for ev in evaluations:
                ev["_id"] = f"{assessment_id}-{student_id}-{ev['qId']}"
                ev["assessmentId"] = assessment_id
                ev["studentId"] = student_id
                ev["approved"] = False
                await db.evaluations.update_one(
                    {"_id": ev["_id"]}, {"$set": ev}, upsert=True
                )

            all_student_evaluations.extend(evaluations)
            print(f"  ✓ {student_name}: {len(evaluations)} answers graded")

        await asyncio.sleep(2.0)  # Smooth transition

        # [Step 2/6] Analysing question paper
        await db.assessments.update_one(
            {"_id": assessment_id},
            {"$set": {"processingStatus": "step_qp"}}
        )

        # If no parsed questions, copy seed questions for this assessment
        if not parsed_questions:
            seed_questions = await db.questions.find({"assessmentId": "asm-001"}).to_list(100)
            for q in seed_questions:
                # Copy fields
                q_copy = dict(q)
                q_copy["_id"] = f"{q_copy['_id']}-{assessment_id}"
                q_copy["assessmentId"] = assessment_id
                await db.questions.update_one(
                    {"_id": q_copy["_id"]},
                    {"$set": q_copy},
                    upsert=True,
                )

        await asyncio.sleep(2.0)

        # [Step 3/6] Extracting concepts & chapters
        await db.assessments.update_one(
            {"_id": assessment_id},
            {"$set": {"processingStatus": "step_concept"}}
        )
        await asyncio.sleep(2.0)

        # [Step 4/6] AI evaluating answers
        await db.assessments.update_one(
            {"_id": assessment_id},
            {"$set": {"processingStatus": "step_eval"}}
        )
        # If parsed answer key exists, apply it to grading
        if parsed_key:
            await _apply_answer_key_grading(db, assessment_id, parsed_key, all_student_evaluations)
        else:
            # Re-read evaluations in case they changed during answer key grading
            all_student_evaluations = await db.evaluations.find({"assessmentId": assessment_id}).to_list(100)

        await asyncio.sleep(2.0)

        # [Step 5/6] Learning gap analysis
        await db.assessments.update_one(
            {"_id": assessment_id},
            {"$set": {"processingStatus": "step_gap"}}
        )

        # Create/update student records for all processed students
        for student_name, student_paths in student_groups.items():
            student_id = f"stu-{assessment_id}-{student_name.lower()}"
            
            # Fetch final evaluations for this specific student
            student_evals = await db.evaluations.find({"assessmentId": assessment_id, "studentId": student_id}).to_list(100)
            student_total = sum(float(ev.get("aiMark", 0.0) or 0.0) for ev in student_evals)

            student_doc = {
                "_id": student_id,
                "name": student_name,
                "roll": f"08-{len(student_groups)}",
                "total": student_total,
                "status": "review",
                "imageUrls": [
                    img.split("media/")[-1] if "media/" in img else img
                    for img in student_paths
                ],
                "assessmentId": assessment_id,
            }
            await db.students.update_one(
                {"_id": student_id},
                {"$set": student_doc},
                upsert=True,
            )

        # Copy interventions
        seed_interventions = await db.interventions.find({"assessmentId": "asm-001"}).to_list(100)
        for i in seed_interventions:
            i_copy = dict(i)
            i_copy["_id"] = f"{i_copy['_id']}-{assessment_id}"
            i_copy["assessmentId"] = assessment_id
            await db.interventions.update_one(
                {"_id": i_copy["_id"]},
                {"$set": i_copy},
                upsert=True,
            )

        await asyncio.sleep(2.0)

        # [Step 6/6] Generating classroom insights
        await db.assessments.update_one(
            {"_id": assessment_id},
            {"$set": {"processingStatus": "step_insights"}}
        )
        await asyncio.sleep(2.0)

        # Recompute assessment aggregates
        final_evals = await db.evaluations.find({"assessmentId": assessment_id}).to_list(1000)
        total_eval_marks = sum(float(ev.get("aiMark", 0.0) or 0.0) for ev in final_evals)
        total_marks_limit = assessment.get("totalMarks", 40) or 40
        
        distinct_students = await db.evaluations.distinct("studentId", {"assessmentId": assessment_id})
        num_students = len(distinct_students) if distinct_students else 1
        
        avg_score_percent = round(((total_eval_marks / num_students) / total_marks_limit) * 100, 1)

        # Update assessment status as complete with real averages
        await db.assessments.update_one(
            {"_id": assessment_id},
            {
                "$set": {
                    "status": "review",
                    "processingStatus": "complete",
                    "totalPapers": num_students,
                    "avgScore": avg_score_percent,
                    "pendingReview": len([e for e in final_evals if e.get("needsReview")]),
                }
            }
        )

    except Exception as e:
        print(f"OCR pipeline error for {assessment_id}: {e}")
        try:
            await _get_db().assessments.update_one(
                {"_id": assessment_id},
                {"$set": {"status": "error", "processingStatus": str(e)[:200]}}
            )
        except Exception:
            pass


async def _apply_answer_key_grading(db, assessment_id: str, parsed_key: list, evaluations: list):
    """Apply parsed answer key to re-grade evaluations."""
    import re
    key_by_q = {}
    for entry in parsed_key:
        q_num = entry.get("questionNumber", 0)
        if q_num:
            key_by_q[q_num] = entry

    for ev in evaluations:
        q_id = ev.get("qId", "")
        q_num = int(q_id[1:]) if q_id.startswith("q") else 0
        key_entry = key_by_q.get(q_num)
        if not key_entry:
            continue

        extracted = (ev.get("studentAnswer") or "").strip()

        # Strategy for MCQs (Q1-Q10)
        if q_num <= 10:
            correct_option = key_entry.get("correctOption")
            if not correct_option:
                # Fallback: extract from correctAnswer text
                correct_ans = key_entry.get("correctAnswer", "")
                m_correct = re.search(r"\b([A-Da-d])\b", correct_and_clean_text := correct_ans.strip()[:5])
                correct_option = m_correct.group(1).upper() if m_correct else None

            # Extract student's selection
            m_stud = re.search(r"\b([A-Da-d])\b", extracted)
            student_option = m_stud.group(1).upper() if m_stud else None

            if correct_option and student_option == correct_option:
                ev["aiMark"] = 1.0
                ev["confidence"] = "high"
                ev["confidenceScore"] = 95
                ev["reasoning"] = f"MCQ match. Student chose correct option {student_option}."
                ev["needsReview"] = False
            else:
                ev["aiMark"] = 0.0
                ev["confidence"] = "high"
                ev["confidenceScore"] = 95
                ev["reasoning"] = f"MCQ mismatch. Student chose {student_option or 'None'}; correct is {correct_option}."
                ev["needsReview"] = False

            # Update in DB
            await db.evaluations.update_one(
                {"_id": ev["_id"]},
                {"$set": {
                    "aiMark": ev["aiMark"],
                    "confidence": ev["confidence"],
                    "confidenceScore": ev.get("confidenceScore", 95),
                    "reasoning": ev["reasoning"],
                    "needsReview": ev["needsReview"],
                }},
            )
            continue

        # Subjective (Q11-Q17)
        expected = key_entry.get("expectedText", "")
        if not expected:
            continue

        # Simple keyword overlap grading
        expected_words = set(expected.lower().split())
        extracted_words = set(extracted.lower().split())
        if expected_words:
            overlap = len(expected_words & extracted_words) / len(expected_words)
            if overlap >= 0.6:
                ev["aiMark"] = float(ev.get("aiMark", 0) or 2)
                ev["confidence"] = "high"
                ev["confidenceScore"] = 85
                ev["reasoning"] = "Answer key match (keyword overlap)."
                ev["needsReview"] = False
            elif overlap >= 0.3:
                ev["aiMark"] = round(float(ev.get("aiMark", 1) or 1), 1)
                ev["confidence"] = "medium"
                ev["confidenceScore"] = 60
                ev["reasoning"] = "Partial answer key match."
                ev["needsReview"] = True
            else:
                ev["aiMark"] = 0.0
                ev["confidence"] = "low"
                ev["confidenceScore"] = 30
                ev["reasoning"] = "Incorrect or no matching keywords."
                ev["needsReview"] = True

        # Update in DB
        await db.evaluations.update_one(
            {"_id": ev["_id"]},
            {"$set": {
                "aiMark": ev["aiMark"],
                "confidence": ev["confidence"],
                "confidenceScore": ev.get("confidenceScore", 50),
                "reasoning": ev["reasoning"],
                "needsReview": ev.get("needsReview", True),
            }},
        )


@router.patch("/{id}", response_model=Assessment)
async def update_assessment(id: str, updates: dict, db=Depends(get_db)):
    result = await db.assessments.update_one({"_id": id}, {"$set": updates})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return await get_assessment(id, db)


@router.get("/{id}/status")
async def get_assessment_status(id: str, db=Depends(get_db)):
    assessment = await db.assessments.find_one({"_id": id}, {"status": 1, "processingStatus": 1})
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return {
        "status": assessment.get("status", "draft"),
        "processingStatus": assessment.get("processingStatus", "pending"),
    }
