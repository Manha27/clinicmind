"""Synthetic patient conversations with questions and gold answers. No real patient data.

Each scenario is a short WhatsApp-style conversation spread over several days. Questions test:
  update_trap : a value changed, so an answer using the OLD value is stale
  commitment  : something the clinic promised
  abstain     : the answer is NOT in the conversation, so the right answer is "I don't know"
  distractor  : a similar-looking fact about someone/something else
Add your own scenarios by copying one and changing it.
"""
from datetime import datetime, timezone


def when(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def q(question, expect, tags=(), stale=(), wrong=()):
    return {"q": question, "expect": expect, "tags": list(tags), "stale": list(stale), "wrong": list(wrong)}


def abstain(question):
    from evals.scoring import ABSTAIN

    return q(question, ABSTAIN, ["abstain"])


SCENARIOS = [
    {
        "id": "priya", "name": "Priya Sharma",
        "messages": [
            ("2026-10-26 09:00", "in", "Hi, I'm interested in dental implants but my budget is around 60k. I'm also nervous about needles."),
            ("2026-10-26 09:10", "out", "Hello Priya! Implants usually start around 90k. Happy to discuss options. When can you visit?"),
            ("2026-10-27 18:30", "in", "I can only come in the evenings after 6 pm, weekends are difficult. My husband will decide the final plan with me."),
            ("2026-10-27 18:40", "out", "Noted! I'll send you EMI options and the implant cost breakdown tomorrow."),
            ("2026-10-29 19:00", "in", "Actually I can stretch to 80k if the plan includes a follow-up. Please call me Friday evening."),
        ],
        "questions": [
            q("What is Priya's current budget?", [["80000"]], ["update_trap"], stale=["60000"]),
            q("Who makes the final decision about the treatment?", [["husband"]]),
            q("What time of day can she visit?", [["evening", "after 6", "6 pm"]]),
            q("What did the clinic promise to send Priya?", [["emi"], ["breakdown"]], ["commitment"]),
            q("Is she anxious about anything?", [["needle"]]),
            abstain("What is her blood group?"),
        ],
    },
    {
        "id": "rahul", "name": "Rahul Verma",
        "messages": [
            ("2026-10-20 11:00", "in", "Hi, I want teeth whitening before my sister's wedding on 15 December. Budget is 8000."),
            ("2026-10-20 11:15", "out", "Sure! Whitening is 7,500. We can book a slot."),
            ("2026-10-22 10:00", "in", "That's slightly high. Can you do it for 6000? I'll pay by UPI."),
            ("2026-10-22 10:20", "out", "Let me check with the doctor and get back to you tomorrow."),
            ("2026-10-23 09:00", "out", "Doctor approved 6000 for you. I've held Saturday 10 am for you."),
            ("2026-10-23 09:30", "in", "Great, I only do mornings. Saturday works."),
        ],
        "questions": [
            q("What occasion does he want the whitening for?", [["wedding"]]),
            q("What price was finally agreed?", [["6000"]], ["update_trap"], stale=["7500", "8000"]),
            q("How does he plan to pay?", [["upi"]]),
            q("Which appointment slot was held for him?", [["saturday"], ["10"]], ["commitment"]),
            q("Does he prefer mornings or evenings?", [["morning"]]),
            abstain("What is his email address?"),
        ],
    },
    {
        "id": "meera", "name": "Meera Kapoor",
        "messages": [
            ("2026-10-15 16:00", "in", "Hello, I'd like braces for my daughter Anika, she is 14. Budget about 45k."),
            ("2026-10-15 16:10", "out", "Happy to help! Consultation is free. Can you come on 5 November?"),
            ("2026-10-16 08:00", "in", "5 November works. Also I'd like a teeth cleaning for myself, same day if possible."),
            ("2026-10-16 08:30", "out", "Yes, we'll do your cleaning right after Anika's consultation."),
            ("2026-10-30 20:00", "in", "Sorry, Anika has exams. Can we move her consultation to 12 November? Please message me on WhatsApp, I don't check email."),
            ("2026-10-31 09:00", "out", "Done: Anika's consultation is moved to 12 November."),
        ],
        "questions": [
            q("Who is the braces treatment for?", [["anika", "daughter"]]),
            q("When is Anika's consultation?", [["12 november", "november 12", "12 nov"]], ["update_trap"], stale=["5 november", "november 5", "5 nov"]),
            q("How should the clinic contact her?", [["whatsapp"]]),
            q("What is the budget for the braces?", [["45000"]]),
            q("What extra treatment did the mother ask for herself?", [["clean"]], ["distractor"]),
            abstain("What allergies does Anika have?"),
        ],
    },
    {
        "id": "vikram", "name": "Vikram Singh",
        "messages": [
            ("2026-11-01 08:30", "in", "I have bad tooth pain, need a root canal urgently. Do you take Star Health insurance?"),
            ("2026-11-01 08:45", "out", "Yes, we accept Star Health. Root canal costs about 12000 before insurance. Can you come today at 5 pm?"),
            ("2026-11-01 09:00", "in", "Today is not possible. Tuesday morning works."),
            ("2026-11-01 09:10", "out", "Booked for Tuesday 10 am. Dr. Kapoor will call you tonight to assess the pain."),
        ],
        "questions": [
            q("What treatment does he need?", [["root canal"]]),
            q("Which insurance provider does he have?", [["star health"]]),
            q("What cost was quoted before insurance?", [["12000"]]),
            q("When is his appointment?", [["tuesday"], ["10"]]),
            q("Who will call him tonight?", [["kapoor"]], ["commitment"]),
            abstain("What is his date of birth?"),
        ],
    },
    {
        "id": "sunita", "name": "Sunita Rao",
        "messages": [
            ("2026-10-10 12:00", "in", "Hi, looking for acne scar laser treatment. I'd prefer a female doctor. Budget is 25k in total."),
            ("2026-10-10 12:30", "out", "We have Dr. Nair. Each laser session is 5000 and most people need 4 sessions."),
            ("2026-10-18 12:00", "in", "My skin doctor elsewhere said I may need 6 sessions. Is there any discount?"),
            ("2026-10-18 12:20", "out", "If you book all 6 sessions upfront we'll give 10 percent off."),
        ],
        "questions": [
            q("What kind of doctor does she prefer?", [["female"]]),
            q("How many sessions does she now expect to need?", [["6", "six"]], ["update_trap"], stale=["4 sessions", "four sessions"]),
            q("What is the price of one laser session?", [["5000"]]),
            q("What discount did the clinic offer?", [["10 percent", "10%", "10 per"]], ["commitment"]),
            q("What is her total budget?", [["25000"]]),
            abstain("What is her phone number?"),
        ],
    },
    {
        "id": "arjun", "name": "Arjun Mehta",
        "messages": [
            ("2026-10-05 14:00", "in", "Hi, Karan referred me. I need a wisdom tooth checked. I work night shifts so afternoons are best."),
            ("2026-10-05 14:20", "out", "Welcome Arjun! Thursday 2 pm is free."),
            ("2026-10-05 14:25", "in", "Thursday 2 pm is perfect. Please send me an SMS reminder the day before."),
            ("2026-10-06 09:00", "out", "Booked, and I'll set the SMS reminder."),
            ("2026-10-09 15:00", "in", "Checked yesterday. Doctor says removal needed, quote please."),
            ("2026-10-09 15:20", "out", "Wisdom tooth removal is 6500. Shall I book it?"),
            ("2026-10-09 15:30", "in", "Let me think. I'll tell you by next week."),
        ],
        "questions": [
            q("Who referred him to the clinic?", [["karan"]]),
            q("What time of day suits him best?", [["afternoon"]]),
            q("How does he want to be reminded?", [["sms"]]),
            q("What was he quoted for the removal?", [["6500"]]),
            q("Has he decided about the removal?", [["think", "next week", "undecided", "not decided", "yet to decide", "not yet"]]),
            abstain("What is his wife's name?"),
        ],
    },
    {
        "id": "neha", "name": "Neha Gupta",
        "messages": [
            ("2026-10-22 10:00", "in", "I am interested in clear aligners. My budget is 1.5 lakh. I live in Chandigarh."),
            ("2026-10-22 10:15", "out", "Clear aligners range from 1.2 to 2 lakh. Want a consultation?"),
            ("2026-10-23 20:00", "in", "Yes, 20 November works. I'd prefer to pay in EMI."),
            ("2026-10-23 20:10", "out", "I'll send you before and after photos of similar cases tomorrow."),
            ("2026-11-02 11:00", "in", "I need to lower my budget to 1.2 lakh, sorry."),
        ],
        "questions": [
            q("What is Neha's current budget?", [["120000"]], ["update_trap"], stale=["150000"]),
            q("Which city does she live in?", [["chandigarh"]]),
            q("How does she prefer to pay?", [["emi"]]),
            q("When is her consultation?", [["20 november", "november 20", "20 nov"]]),
            q("What did the clinic promise to send Neha?", [["photo"]], ["commitment"]),
            abstain("What is her age?"),
        ],
    },
    {
        "id": "imran", "name": "Imran Khan",
        "messages": [
            ("2026-10-12 10:00", "in", "Need an implant for a missing molar. Budget 90k."),
            ("2026-10-12 10:10", "out", "Our implant package is 85000 including crown."),
            ("2026-10-20 17:00", "in", "Another clinic, Smile Care, quoted 70k. Can you match?"),
            ("2026-10-20 17:20", "out", "We can take 5000 off, so 80000. We'll call you Monday to follow up."),
            ("2026-10-21 09:00", "in", "I'll discuss with my brother and let you know."),
        ],
        "questions": [
            q("What is the clinic's latest offered price?", [["80000"]], ["update_trap"], stale=["85000"]),
            q("Which competitor clinic did he mention?", [["smile care"]], ["distractor"]),
            q("What did the clinic promise to do on Monday?", [["call"]], ["commitment"]),
            q("Who will he consult before deciding?", [["brother"]]),
            q("What is his budget?", [["90000"]]),
            abstain("What is his job?"),
        ],
    },
]


def total_questions(scenarios=SCENARIOS) -> int:
    return sum(len(s["questions"]) for s in scenarios)
