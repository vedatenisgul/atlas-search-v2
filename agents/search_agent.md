# Search Agent

**Role:** Search Pipeline Engineer  
**Tool:** Cursor Composer  
**Phase:** Phase 4

---

## Responsibility

Owns the query processing pipeline. Does not touch crawling, storage internals, or UI.

## Prompt

> "You are the Search Agent. Build the search pipeline for Atlas Search. Files to create: search/engine.py, search/ranking.py.
>
> Requirements:
> - SearchEngine.query(query, limit, offset) is a static method
> - Turkish locale case-folding: dotted I -> lowercase i, dotless I -> lowercase i, plus umlaut/cedilla/breve variants
> - Tokenize on whitespace, strip all string.punctuation characters
> - Exact-match Trie lookup per token via trie_db.search()
> - Aggregate per URL: sum term_frequency, take min depth across all matched tokens
> - Pass aggregated dict to rank_results()
> - Paginate: results[offset : offset + limit]
> - Hydrate: enrich each result with title and snippet from db.data['metadata']
> - Return list of (url, origin_url, depth, frequency, relevance_score) tuples
> - ranking.py: score = (freq x 10) + 1000 - (depth x 5), sort descending"

## Inputs

- Indexer Agent Trie and NoSQLStore API documentation

## Outputs

- `search/engine.py` — query pipeline with Turkish folding, aggregation, pagination, hydration
- `search/ranking.py` — relevance formula and descending sort

## Issues Raised

- TF-IDF would produce better relevance at scale than raw frequency
- No fuzzy or prefix search exposed to users

## Orchestrator Response

TF-IDF deferred and added to recommendation.md. Prefix search noted as future enhancement.
