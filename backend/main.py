from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
import os, shutil, fitz
from uuid import uuid4
from sentence_transformers import SentenceTransformer
import faiss
import numpy as np
from openai import OpenAI
import traceback
from dotenv import load_dotenv

# ------------------ APP SETUP ------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploaded_pdfs"
os.makedirs(UPLOAD_DIR, exist_ok=True)

embedding_store = {}

# 🧠 MEMORY STORES
chat_history_store = {}   # per doc_id
user_memory_store = {}    # per doc_id

# ------------------ GROQ SETUP ------------------
load_dotenv()

client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

def generate_answer(messages):
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        temperature=0.3,
    )
    return response.choices[0].message.content.strip()

# ------------------ EMBEDDING MODEL ------------------
print("🔎 Loading embedding model...")
embedder = SentenceTransformer("BAAI/bge-base-en-v1.5")
print("✅ Embedding model loaded.")

# ------------------ CHUNKING ------------------
def chunk_text(text, chunk_size=300, overlap=80):
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size - overlap):
        chunks.append(" ".join(words[i : i + chunk_size]))
    return chunks

# ------------------ MEMORY EXTRACTOR ------------------
def extract_memory(query: str):
    q = query.lower()
    if "my name is" in q:
        name = query.split("is")[-1].strip()
        return {"key": "name", "value": name}
    return None

# ------------------ UPLOAD ------------------
@app.post("/upload/")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.endswith(".pdf"):
        return {"error": "Only PDF files are allowed."}

    doc_id = str(uuid4())
    file_path = os.path.join(UPLOAD_DIR, f"{doc_id}.pdf")

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    all_chunks = []

    try:
        with fitz.open(file_path) as doc:
            for page in doc:
                text = page.get_text()
                if text.strip():
                    chunks = chunk_text(text)
                    all_chunks.extend(chunks)
    except Exception as e:
        return {"error": f"PDF parsing failed: {e}"}

    if not all_chunks:
        return {"error": "No extractable text found."}

    try:
        embeddings = embedder.encode(all_chunks, normalize_embeddings=True)
        dimension = embeddings.shape[1]

        index = faiss.IndexFlatIP(dimension)
        index.add(np.array(embeddings))

        embedding_store[doc_id] = {"index": index, "texts": all_chunks}

    except Exception as e:
        return {"error": f"Embedding failed: {e}"}

    return {
        "doc_id": doc_id,
        "chunks": len(all_chunks),
    }

# ------------------ ASK ------------------
@app.post("/ask/")
async def ask_question(doc_id: str = Form(...), question: str = Form(...)):
    try:
        if doc_id not in embedding_store:
            return {"error": "Invalid document ID."}

        # -------- INIT MEMORY --------
        if doc_id not in chat_history_store:
            chat_history_store[doc_id] = []

        if doc_id not in user_memory_store:
            user_memory_store[doc_id] = {}

        chat_history = chat_history_store[doc_id]
        user_memory = user_memory_store[doc_id]

        # -------- EXTRACT MEMORY --------
        mem = extract_memory(question)
        if mem:
            user_memory[mem["key"]] = mem["value"]

        # -------- EMBED QUERY --------
        query_embedding = embedder.encode([question], normalize_embeddings=True)

        index = embedding_store[doc_id]["index"]
        texts = embedding_store[doc_id]["texts"]

        D, I = index.search(np.array(query_embedding), k=10)

        retrieved_chunks = [texts[i] for i in I[0] if i < len(texts)]
        context = "\n".join(retrieved_chunks[:5])

        # -------- MEMORY CONTEXT --------
        memory_context = ""
        if "name" in user_memory:
            memory_context += f"User name: {user_memory['name']}\n"

        # -------- CHAT HISTORY --------
        history_messages = chat_history[-6:]  # last 3 exchanges

        # -------- SYSTEM PROMPT --------
        system_prompt = f"""
You are a smart and friendly assistant.

Rules:
- Prefer user memory over everything.
- Use chat history for conversation continuity.
- Use context only if relevant.
- Otherwise use general knowledge.

Keep answers:
- Short (2–3 lines)
- Natural and human-like
- No tags like <final answer>
"""

        # -------- BUILD MESSAGES --------
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": f"User Memory:\n{memory_context}"},
            {"role": "system", "content": f"Context:\n{context}"},
        ]

        messages.extend(history_messages)
        messages.append({"role": "user", "content": question})

        # -------- GENERATE --------
        answer = generate_answer(messages)

        # -------- UPDATE HISTORY --------
        chat_history.append({"role": "user", "content": question})
        chat_history.append({"role": "assistant", "content": answer})

        return {"answer": answer}

    except Exception as e:
        trace = traceback.format_exc()
        print("❌ ERROR:", trace)
        return {"error": "Internal server error", "details": str(e)}