from app.llm import embed, generate

MESSAGE = (
    "Hi, I'm interested in implants but my budget is around 60k. "
    "I'm also nervous about needles."
)

reply = generate(
    f"In one sentence, what does this patient enquiry tell the clinic?\n\n{MESSAGE}",
    purpose="try",
)
print("LLM:", reply)

vectors = embed(["budget around 60k", "afraid of needles", "wants evening slots"])
print("Embeddings:", len(vectors), "vectors of length", len(vectors[0]))