## Executive One-Pager: AISICH BOT APP

### 1. System Overview

An automated, multi-tenant ChatAPP AI Bot solution built on a lightweight, cost-optimized Heroku & AWS microservices architecture.
The system receives ChatAPP messages via Evolution API, enforces strict per-user authorization against a central PostgreSQL database, enriches query prompts using an in-memory vector search engine (FAISS) pre-computed on AWS S3, and routes the final context to an LLM (Hugging Face) using the individual user's personal API key.

### 2. Key Business & Technical Features

* Strict Per-User Authorization: Every incoming message is validated against PostgreSQL. Unregistered users receive an immediate cutoff response without invoking any LLM costs.
* Per-User API Key Isolation: Instead of using a single global LLM key, the bot dynamically retrieves and applies the caller's unique API key stored in PostgreSQL.
* Zero-DB Vector RAG (Retrievable Augmented Generation): The knowledge base context is shared across all 1k–2k users but computed locally and stored on AWS S3. The Eco Dyno loads the vector index into RAM at startup, avoiding expensive vector database costs or CPU overload on PostgreSQL.
* Asynchronous Webhook Processing: Webhooks return instant HTTP 200 OK status to ChatAPP via FastAPI background tasks, preventing message retry loops or timeouts during LLM processing.
* Cost & Database Isolation: Shared database instances across Heroku apps keep infrastructure costs minimal while isolating ChatAPP connection state from core business logic.

### 3. End-to-End System Architecture & Data Flow

```text
[ User on ChatAPP ]
        │ (Message / Group Tag)
        ▼
[ aisich-evolution-app ] (Heroku Container)
        │ (HTTP Webhook POST)
        ▼
[ aisich-bot-app ] (Heroku FastAPI)
        │
        ├── 1. Extract Sender ID (phone/remoteJid)
        ├── 2. Query PostgreSQL (SELECT api_key WHERE phone = :id AND active = true)
        │        ├── NOT FOUND ──► Send "Not Registered" via Evolution API ──► END
        │        └── FOUND ──────► Retrieve User's LLM Key
        │
        ├── 3. Perform RAG (In-Memory FAISS Vector Search on pre-loaded S3 context)
        │        └── Returns Top 3 Relevant Context Chunks
        │
        ├── 4. Call Hugging Face / LLM API (Prompt = Context + User Question, Auth = User API Key)
        │
        └── 5. Post Final Answer ──► [ aisich-evolution-app ] ──► [ ChatAPP User ]
```

### 4. Infrastructure & Technology Stack

```text
Layer
|-- Technology / Service
|-- Role & Responsibility
|-- ChatAPP Gateway
|-- aisich-evolution-app (Heroku)
|-- Runs Evolution API in Docker to maintain ChatAPP WebSockets and trigger webhooks.
|-- Application Server
|-- aisich-bot-app (FastAPI / Eco Dyno)
|-- Stateless web server handling user verification, vector search, LLM calls, and response routing.
|-- Database (Source of Truth)
|-- Heroku PostgreSQL (Essential Tier)
|-- Stores users table (phone_number, api_key, is_active). Fast sub-1ms B-tree indexed lookups.
|-- Session & Queue Cache
|-- Heroku Redis
|-- Handles session management and background task queues for Evolution API.
|-- Vector Index Storage
|-- AWS S3 (boto3)
|-- Hosts pre-computed binary vector files (index.faiss and chunks.pkl) downloaded at Dyno startup.
|-- Vector Engine / Model
|-- FAISS + all-MiniLM-L6-v2
|-- Converts user queries to 384-dim embeddings and performs in-memory cosine similarity search.
|-- Inference Engine
|-- Hugging Face Inference API
|-- Generates natural language responses using the user's specific API key.
```

### 5. Resource Profile & Cost Optimization

### Heroku Eco Dyno RAM Quota: 512 MB

```text
┌─────────────────────────────────────────────────────────┐
│ [ FastAPI + Python Runtime ]   ~60 MB                    │
│ [ SentenceTransformers Model]  ~120 MB                   │
│ [ FAISS Vector Index (S3) ]    ~20 MB                    │
│ [ DB Drivers & Buffer ]        ~10 MB                    │
├─────────────────────────────────────────────────────────┤
│ TOTAL ESTIMATED MEMORY USAGE:  ~210 MB / 512 MB        │
│ REMAINING FREE RAM BUFFER:     ~302 MB                  │
└─────────────────────────────────────────────────────────┘
```

* Database Overhead: < 1% CPU utilization. Postgres only processes standard key-value string lookups, preserving connection limits.
* Vector Search Latency: < 2 ms per request (executed entirely in Dyno RAM).
* Monthly Cost Footprint: Minimal — runs within 1 Heroku Eco subscription ($5/mo), shared Postgres/Redis add-ons, and AWS S3 free tier usage.

## App Operational flow

```text
[User Tags Bot on chat app]
         │
         ▼
[Evolution API forwards Webhook to FastAPI]
         │
         ▼
[FastAPI extracts Sender ID & Question]
         │
         ▼
[Lookup Sender ID in PostgreSQL]
         │
  ┌──────┴────────────────────────┐
  │                               │
  ▼                               ▼
[API Key NOT Found]              [API Key Found]
  │                               │
  ├─► Send Evolution API:         ├─► Load Static Context
  │   "User is not registered"    ├─► Call LLM using User's API Key
  │                               ├─► Send LLM Reply via Evolution API
  ▼                               ▼
[End Process]                    [End Process]
```

## NOTE: set web concurrency to 1

```bash
heroku config:set WEB_CONCURRENCY=1 -a aisich-bot-app
```