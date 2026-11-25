"""Load the demo patients (the same synthetic conversations the evals use).

    python -m scripts.seed_demo               # create leads + messages, queue extraction (run the worker)
    python -m scripts.seed_demo --stepwise    # replay day by day, extracting after each day (slow, uses the LLM)
                                              # this is the mode that shows a SUPERSEDE: 60k replaced by 80k
    python -m scripts.seed_demo --reset       # delete previously seeded demo leads first

Safe to run twice: leads that already exist are skipped. All data is synthetic.
"""
import argparse
from itertools import groupby

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.jobs.queue import enqueue_extraction
from app.memory.service import extract_pending
from app.models import Lead, Memory, MemoryEvent, Message, Org, Thread
from evals.fixtures import SCENARIOS, when

DOMAIN = "@example.com"


def get_or_create_org(db) -> Org:
    org = db.scalar(select(Org).order_by(Org.created_at).limit(1))
    if org is None:
        org = Org(name="Demo Clinic")
        db.add(org)
        db.commit()
    return org


def reset(db, org) -> int:
    leads = db.scalars(select(Lead).where(Lead.org_id == org.id, Lead.email.like(f"%{DOMAIN}"), Lead.source == "demo")).all()
    ids = [lead.id for lead in leads]
    if ids:
        memory_ids = select(Memory.id).where(Memory.scope_id.in_(ids))
        db.execute(delete(MemoryEvent).where(MemoryEvent.memory_id.in_(memory_ids)))
        db.execute(delete(Memory).where(Memory.scope_id.in_(ids)))
        db.execute(delete(Lead).where(Lead.id.in_(ids)))  # messages, threads, tasks cascade
        db.commit()
    return len(ids)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stepwise", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--only", nargs="+", help="scenario ids, e.g. priya rahul")
    args = parser.parse_args()

    with SessionLocal() as db:
        org = get_or_create_org(db)
        if args.reset:
            print(f"Removed {reset(db, org)} demo leads")

        for scenario in SCENARIOS:
            if args.only and scenario["id"] not in args.only:
                continue
            email = f"{scenario['id']}{DOMAIN}"
            if db.scalar(select(Lead.id).where(Lead.org_id == org.id, Lead.email == email)):
                print(f"skip {scenario['name']} (already exists)")
                continue
            lead = Lead(org_id=org.id, full_name=scenario["name"], email=email, source="demo")
            db.add(lead)
            db.flush()
            thread = Thread(lead_id=lead.id, channel="whatsapp")
            db.add(thread)
            db.commit()

            if args.stepwise:
                for _, day in groupby(scenario["messages"], key=lambda m: m[0][:10]):
                    for stamp, direction, text in day:
                        db.add(Message(thread_id=thread.id, lead_id=lead.id, direction=direction, body=text, sent_at=when(stamp)))
                    db.commit()
                    created = extract_pending(db, lead)
                    print(f"  {scenario['name']}: {len(created)} new memories")
            else:
                for stamp, direction, text in scenario["messages"]:
                    db.add(Message(thread_id=thread.id, lead_id=lead.id, direction=direction, body=text, sent_at=when(stamp)))
                enqueue_extraction(db, lead.id)
                db.commit()
            print(f"added {scenario['name']} ({len(scenario['messages'])} messages)")

    print("\nDone. " + ("" if args.stepwise else "Start the worker to extract memories:  python -m app.worker"))


if __name__ == "__main__":
    main()
