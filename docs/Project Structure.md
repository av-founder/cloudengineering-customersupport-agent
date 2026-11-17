your-project/
  README.md                 setup and run instructions
  requirements.txt          pinned dependencies
  .env.example              every variable, placeholder values only
  .gitignore                must include .env and storage/
  src/
    ingest.py               normalise tickets from all four channels
    classify.py             intent and urgency with confidence
    retrieve.py             vector search over the documentation
    route.py                the escalation decision and its threshold
    generate.py             answer drafting with citations
    guardrails.py           the checks that can block a response
    logging_store.py        the decision log
    api.py                  the FastAPI application
  prompts/
    build/                  prompts that run inside the system
    evaluation/             prompts used to judge output
    README.md               the register, with versions
  tests/
  evaluation/
    harness.py              runs the hidden evaluation set end to end
    results/                dated output from each run
  docs/
    architecture.md
  data/                     small samples only
  storage/                  generated at runtime, never committed
  .github/workflows/ci.yml
