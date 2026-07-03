from fastapi import APIRouter, Depends, HTTPException
from typing import List
from backend.core.database import get_db
from backend.models.student import Student

router = APIRouter()

@router.get("/{id}/students")
async def get_students(id: str, db=Depends(get_db)):
    students = await db.students.find({"assessmentId": id}).to_list(100)
    for s in students:
        s["id"] = s.get("_id", "")
    return students

@router.get("/{id}/students/{sid}/profile")
async def get_student_profile(id: str, sid: str, db=Depends(get_db)):
    # Mocking profile for the student
    return {
        "strong": [
            {"concept": "Crop Production", "chapter": "ch3", "mastery": 92},
            {"concept": "Irrigation", "chapter": "ch3", "mastery": 90}
        ],
        "developing": [
            {"concept": "Fertilization", "chapter": "ch4", "mastery": 60}
        ],
        "weak": [
            {"concept": "IVF", "chapter": "ch4", "mastery": 30}
        ],
        "misconceptions": [
            "Student understands the source of gametes but cannot explain their role in fertilization."
        ]
    }

@router.get("/{id}/students/{sid}/term-trends")
async def get_term_trends(id: str, sid: str, db=Depends(get_db)):
    return [
        {"assessmentId": "asm-003", "name": "FA2 — Cell Structure", "date": "2026-01-05", "totalMarks": 25, "studentScore": 14, "classAvg": 18.1},
        {"assessmentId": "asm-001", "name": "SA1 — Biological Science", "date": "2026-02-08", "totalMarks": 40, "studentScore": 28, "classAvg": 27.4}
    ]

@router.get("/{id}/students/{sid}/concept-trends")
async def get_concept_trends(id: str, sid: str, db=Depends(get_db)):
    return [
        {"concept": "Fertilization", "chapter": "ch4", "history": [40, 45, 55, 60], "trend": "up", "delta": 20}
    ]
