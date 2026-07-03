# Learning Agent Architecture Design

Date: 2026-07-03

## Scope

This document defines the MVP architecture for the OralSage learning-agent system. The MVP focuses on a backend and terminal demo flow for course learning:

```text
user wants to learn a topic
-> system creates or reads the user profile
-> system plans a course path from the existing KAG knowledge base
-> system generates learning resources from one shared outline
-> tutor teaches step by step
-> user can interrupt with questions without changing course progress
-> user answers questions
-> assessment updates mastery and learning memory
-> tutor continues, reinforces, or adds practice
```

The MVP does not build a new frontend page, video generation, a separate Evaluator Agent, a new retrieval algorithm, or a new reranker integration.

## Current Project Context

OralSage already contains a backend FastAPI application with service-oriented agents and a master router. Existing modules include pathology QA, clinical decision support, literature search, image analysis, knowledge graph access, question-bank linking, and clinic statistics.

The learning system should reuse the existing KAG infrastructure rather than add new retrieval logic. The current KAG knowledge architecture includes:

- Evidence chunks from textbook OCR, clinical authority sources, and external references.
- KG nodes, KG edges, concept edges, and KG-to-chunk links.
- Question-bank records linked to KG nodes.
- A unified retrieval corpus.
- Graph, sparse BM25, and dense BGE-M3 indexes.
- Existing retrieval operators: alias seed, sparse BM25, dense retrieval, graph local expansion, evidence linking, reverse evidence trace, hybrid merge, and question-KG linking.

The learning layer should add orchestration, session state, resource generation, and assessment on top of those capabilities.

## Recommended MVP Architecture

The MVP uses five agents, two internal services, and one resource factory:

```text
Master Orchestrator Agent
User Model Agent
Planner Agent
Tutor Agent
QA Agent
Resource Factory
Assessment Engine
Learning Memory
```

### Master Orchestrator Agent

The Master Orchestrator handles user intent and session control. It decides whether the request is:

- Starting a course.
- Continuing the current lesson.
- Asking an interruption question.
- Submitting an answer.
- Requesting a summary.
- Viewing resources.

It creates or loads session state, calls the appropriate agent or service, records events, handles recoverable errors, and returns a structured result to the API or CLI.

### User Model Agent

The User Model Agent maintains the learner profile. The profile includes:

- Knowledge level.
- Learning goal or target exam.
- Preferred explanation style.
- Learning pace.
- Known weak points.
- Mistake patterns.
- Confidence in the profile.

Initial profile creation uses a quick diagnosis flow. If the user has no profile, the system asks a small set of questions or allows skipping to a low-confidence default profile.

Example initial profile:

```json
{
  "level": "本科高年级",
  "goal": "期末考试",
  "preferred_explanation": "病例带入",
  "pace": "标准",
  "known_weak_points": [],
  "mistake_patterns": [],
  "target_exam": null,
  "confidence": 0.6
}
```

If diagnosis is skipped, the system uses:

```json
{
  "level": "unknown",
  "goal": "course_learning",
  "preferred_explanation": "concept_then_case",
  "pace": "standard",
  "known_weak_points": [],
  "mistake_patterns": [],
  "target_exam": null,
  "confidence": 0.2
}
```

The profile is updated incrementally from learning events. For example, repeated mechanism questions can update explanation preference, and repeated diagnostic mistakes can update weak points.

### Planner Agent

The Planner Agent creates a personalized learning path from:

- User topic.
- User profile.
- Current mastery state.
- Existing KAG retrieval and KG context.

The Planner should prefer KG/KAG structure. If KG node matching is weak, it may use existing hybrid retrieval evidence to synthesize a temporary outline, marked with a warning in trace metadata.

The Planner outputs both a course outline and mindmap JSON. The mindmap is produced as data and can later be rendered by code with Mermaid, markmap, or another renderer. It does not require a separate Mindmap Agent.

### Tutor Agent

The Tutor Agent manages step-by-step teaching. It reads the current outline node and learning memory, then generates:

- A short teaching segment.
- A question or micro-check.
- Feedback after assessment.
- Reinforcement or extra practice when needed.

The Tutor does not own persistent state. It reads state from Learning Memory and writes events through the Master Orchestrator.

### QA Agent

The QA Agent answers interruption questions during a lesson. It uses:

- The current outline node.
- Current session context.
- User profile.
- Existing KAG evidence and retrieval.

The QA Agent must not advance `current_outline_node_id`. It can record confusion tags, related KG nodes, and whether the Tutor should reinforce the topic later.

### Resource Factory

The Resource Factory is not an agent. It is a reusable backend workflow that receives the Planner outline and generates course resources from the same shared outline.

MVP resources:

- `handout.md`
- `slides.json`
- `course.pptx`
- `base_questions.json`
- `reading_list.json`
- `mindmap.json`

PPT generation should use a structured intermediate format first. The recommended MVP implementation is:

```text
outline -> slides.json -> PptxGenJS -> course.pptx
```

The first PPTX version only needs to be an editable structured draft with titles, bullet points, speaker notes, and citations. Visual polish is not required for MVP.

If PPTX export fails, the system should preserve `slides.json`, mark the resource status as partial success, and continue the lesson flow.

### Assessment Engine

The Assessment Engine is an automatically triggered backend code service, not a standalone chat agent. It is called after learning events such as:

- User submits an answer.
- User asks an interruption question.
- A section is completed.
- A session summary is requested.

For objective questions, the engine grades deterministically from the stored answer key. For short answers or clinical cases, it may call an LLM and require structured grading output.

The Assessment Engine updates:

- Answer records.
- Mastery scores.
- Mistake types.
- Weak points.
- Recommendations such as continue, reinforce, or add practice.

Example output:

```json
{
  "question_id": "q_1",
  "is_correct": false,
  "score": 0,
  "mistake_type": "概念混淆",
  "affected_kg_node_ids": ["kg:xxx"],
  "mastery_delta": -0.05,
  "recommendation": {
    "action": "reinforce",
    "reason": "用户混淆牙龈炎与牙周炎的核心区别"
  }
}
```

### Learning Memory

Learning Memory is the SQLite-backed persistent state layer. It stores user profiles, sessions, outlines, resources, events, mastery records, and answers.

The main reason for this layer is to make course progress stable. Interruption QA can update memory, but it must not mutate the lesson progress pointer.

## SQLite Data Model

The MVP should use event history plus current-state snapshots.

### users

```text
id
display_name
created_at
```

### user_profiles

```text
user_id
profile_json
confidence
updated_at
```

### learning_sessions

```text
id
user_id
topic
mode
status
current_outline_node_id
created_at
updated_at
```

`current_outline_node_id` is the stable lesson progress pointer. QA interruptions must not change it.

### lesson_outlines

```text
id
session_id
outline_json
mindmap_json
source_trace_json
created_at
```

### generated_resources

```text
id
session_id
resource_type
resource_path
resource_json
source_trace_json
created_at
```

Allowed `resource_type` values for MVP:

```text
handout_md
slides_json
pptx
base_questions_json
reading_list_json
mindmap_json
```

### lesson_events

```text
id
session_id
user_id
event_type
outline_node_id
payload_json
created_at
```

MVP event types:

```text
lesson_started
resource_generated
tutor_step_presented
user_interruption_question
qa_answered
question_presented
answer_submitted
assessment_completed
mastery_updated
node_completed
session_summary_generated
```

### mastery_records

```text
id
user_id
session_id
kg_node_id
concept_name
mastery_score
confidence
evidence_json
updated_at
```

### answer_records

```text
id
session_id
user_id
question_id
outline_node_id
user_answer
grading_json
is_correct
score
created_at
```

## Agent Result Protocol

All agents should return a JSON-serializable result:

```json
{
  "ok": true,
  "agent": "planner",
  "message": "Optional user-facing text.",
  "data": {},
  "events": [],
  "next_action": {
    "type": "continue",
    "payload": {}
  },
  "trace": {
    "model": "qwen-plus",
    "sources": [],
    "warnings": []
  }
}
```

Natural-language content must stay in fields such as `message`, `teaching_text`, or `answer`. Control fields must remain parseable JSON.

## Core Resource Formats

### Planner Outline

```json
{
  "title": "牙周炎系统学习路径",
  "nodes": [
    {
      "id": "node_1",
      "title": "牙周炎的定义与疾病本质",
      "kg_node_ids": ["kg:xxx"],
      "learning_objectives": ["能说出牙周炎与牙龈炎的核心区别"],
      "prerequisites": [],
      "estimated_minutes": 8,
      "difficulty": "basic",
      "source_refs": []
    }
  ]
}
```

### slides.json

```json
{
  "title": "牙周炎系统学习",
  "slides": [
    {
      "type": "title",
      "title": "牙周炎系统学习",
      "subtitle": "从病因到临床判断"
    },
    {
      "type": "content",
      "title": "牙周炎的疾病本质",
      "bullets": ["慢性炎症性疾病", "破坏牙周支持组织"],
      "speaker_notes": "这里重点强调牙龈炎和牙周炎的分界。",
      "source_refs": []
    }
  ]
}
```

### base_questions.json

```json
{
  "questions": [
    {
      "id": "q_1",
      "type": "single_choice",
      "outline_node_id": "node_1",
      "kg_node_ids": ["kg:xxx"],
      "stem": "牙周炎区别于牙龈炎的关键在于？",
      "options": ["牙龈出血", "牙槽骨吸收", "菌斑存在", "口臭"],
      "answer": "B",
      "explanation": "牙周炎累及牙周支持组织破坏。",
      "difficulty": "basic",
      "source_refs": []
    }
  ]
}
```

### Tutor Step

```json
{
  "outline_node_id": "node_1",
  "teaching_text": "我们先把牙周炎看成一个从菌斑触发到支持组织破坏的过程……",
  "question": {
    "id": "q_1",
    "type": "single_choice",
    "stem": "...",
    "options": []
  },
  "expected_user_action": "answer",
  "progress": {
    "current_index": 1,
    "total": 6
  }
}
```

### QA Result

```json
{
  "answer": "你问的是牙槽骨吸收的机制，可以理解为炎症因子促进破骨细胞活化……",
  "related_outline_node_id": "node_2",
  "related_kg_node_ids": ["kg:xxx"],
  "confusion_tags": ["机制链条不完整"],
  "should_tutor_reinforce_later": true,
  "source_refs": []
}
```

## Backend Module Layout

Add a new learning service package:

```text
backend/services/learning/
  __init__.py
  orchestrator.py
  user_model.py
  planner.py
  tutor.py
  qa.py
  resource_factory.py
  assessment.py
  memory.py
  schemas.py
  prompts/
    planner.md
    tutor.md
    qa.md
    user_model.md
    handout.md
    slides.md
```

This layout follows the existing backend style of service modules and prompt files.

## API Design

Add the following endpoints:

```text
POST /api/learning/profile/diagnose
POST /api/learning/session/start
POST /api/learning/session/{session_id}/next
POST /api/learning/session/{session_id}/ask
POST /api/learning/session/{session_id}/answer
GET  /api/learning/session/{session_id}/summary
GET  /api/learning/session/{session_id}/resources
```

### Start Session

Request:

```json
{
  "user_id": "demo",
  "topic": "牙周炎",
  "profile_mode": "use_existing_or_default"
}
```

Flow:

```text
read or create user profile
-> Planner creates outline and mindmap
-> Resource Factory creates handout, slides, PPTX, questions, reading list
-> Learning Memory saves session and resources
-> Tutor returns first teaching step
```

### Ask Interruption Question

Request:

```json
{
  "question": "为什么牙周炎会导致牙槽骨吸收？"
}
```

Flow:

```text
QA answers using current session context and KAG evidence
-> record user_interruption_question and qa_answered
-> update confusion memory
-> do not change current_outline_node_id
```

### Submit Answer

Request:

```json
{
  "question_id": "q_1",
  "answer": "B"
}
```

Flow:

```text
Assessment Engine grades answer
-> update answer_records and mastery_records
-> Tutor returns feedback and next recommended action
```

### Continue Lesson

Request:

```json
{
  "action": "continue"
}
```

Flow:

```text
if current node needs reinforcement, Tutor reinforces or adds practice
else advance current_outline_node_id
-> Tutor teaches next step
```

## CLI Demo

Add a terminal demo script:

```text
scripts/learning_demo.py
```

Example:

```text
python scripts/learning_demo.py --user demo --topic 牙周炎
```

Commands:

```text
/profile
/start
/next
/ask 问题
/answer A
/resources
/summary
/quit
```

The terminal demo must prove:

1. User profile creation or loading.
2. Course outline and resources generation.
3. Tutor step output with a question.
4. Interruption QA without progress mutation.
5. Answer grading and mastery update.
6. Continue behavior after assessment.
7. Session summary generation.

## Error Handling

- If KAG node matching is weak, Planner uses existing hybrid retrieval evidence and records a warning.
- If an LLM call fails, Master retries once where safe and then returns a structured recoverable error.
- If PPTX generation fails, keep `slides.json`, mark PPTX as partial failure, and continue.
- If no user profile exists, create a default low-confidence profile unless diagnosis is explicitly required.
- If an answer references an unknown `question_id`, return a recoverable error and do not update progress.
- If a resource already exists for a session, Resource Factory may reuse it unless regeneration is explicitly requested.

## MVP Milestones

### Phase 1: Learning Memory And Session Skeleton

Implement SQLite-backed memory, profile creation/loading, session creation, event recording, and a minimal CLI.

Acceptance:

```text
python scripts/learning_demo.py --user demo --topic 牙周炎
-> creates user/profile/session
-> writes SQLite records
-> summary can read current state
```

### Phase 2: Planner And Resource Factory

Implement outline and resource generation using existing KAG services where available.

Resources:

- `handout.md`
- `slides.json`
- `course.pptx`
- `base_questions.json`
- `reading_list.json`
- `mindmap.json`

Acceptance:

```text
/start 牙周炎
-> creates session resource files under data/learning/sessions/{session_id}/
-> saves generated_resources records
```

### Phase 3: Tutor And QA Interruption

Implement Tutor step generation and QA interruption handling.

Acceptance:

```text
/start 牙周炎
/ask 为什么会牙槽骨吸收？
-> QA answers
-> summary shows current outline node unchanged
/next
-> continues original lesson path
```

### Phase 4: Assessment And Dynamic Adjustment

Implement objective question grading, mastery updates, answer records, Tutor feedback, and summary reporting.

Acceptance:

```text
/answer B
-> returns correctness, explanation, and mastery change
/summary
-> shows weak points and mastery state
/next
-> continues, reinforces, or adds practice based on assessment
```

## Test Plan

Add focused tests and smoke scripts:

```text
test_learning_memory.py
test_learning_orchestrator.py
test_resource_factory.py
scripts/smoke_test_learning.py
```

Minimum assertions:

- Creating user/profile/session works.
- Event recording works.
- Start session creates outline and resource records.
- Interruption QA does not change `current_outline_node_id`.
- Submitting an answer updates `answer_records` and `mastery_records`.
- Resource JSON formats are schema-valid.

## Non-Goals For MVP

- Frontend learning classroom page.
- Video generation.
- Separate Evaluator Agent.
- New retrieval operators.
- Reranker model integration.
- Multi-user authentication or permissions.
- Long-term visual learning analytics.
- Production-grade PPT design polish.

## Future Extensions

After MVP, the architecture can evolve naturally:

- Promote Assessment Engine to an Evaluator Agent when evaluation becomes more complex.
- Add a frontend classroom page with outline, chat, resources, and mastery view.
- Improve PPT output using templates or a stronger open-source PPT generation stack.
- Add video or animation generation as a Resource Factory extension.
- Add reranker support inside the existing retrieval layer if retrieval quality requires it.
- Add richer learning analytics from the event stream.

