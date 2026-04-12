from ddgs import DDGS
import ollama
import time

MODEL = "qwen2.5:7b"


def fetch_ai_news(max_results=5):
    query = "latest AI news today"
    backends = ["yahoo", "bing", "duckduckgo"]

    for backend in backends:
        try:
            with DDGS() as ddgs:
                results = []
                for r in ddgs.news(query, max_results=max_results, backend=backend):
                    results.append({
                        "title": r.get("title", ""),
                        "body": r.get("body", ""),
                        "url": r.get("url", "") or r.get("href", ""),
                        "source": r.get("source", backend),
                        "date": r.get("date", "")
                    })
                if results:
                    return results
        except Exception:
            time.sleep(1)

    try:
        with DDGS() as ddgs:
            results = []
            for r in ddgs.text(query, max_results=max_results, backend="bing"):
                results.append({
                    "title": r.get("title", ""),
                    "body": r.get("body", ""),
                    "url": r.get("href", "") or r.get("url", ""),
                    "source": "web search",
                    "date": ""
                })
            return results
    except Exception:
        return []


def summarize_ai_news(results):
    if not results:
        return "I couldn't fetch AI news right now. The news source may be rate-limiting requests."

    formatted = []
    for i, r in enumerate(results, start=1):
        formatted.append(
            f"{i}. {r['title']}\n"
            f"Source: {r['source']}\n"
            f"Date: {r['date']}\n"
            f"Summary: {r['body']}\n"
            f"URL: {r['url']}"
        )

    prompt = (
        "Summarize these AI news items into a short Telegram-friendly digest. "
        "Keep it concise and easy to scan. Use bullet points and mention why each item matters.\n\n"
        + "\n\n".join(formatted)
    )

    response = ollama.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": "You are an AI news assistant."},
            {"role": "user", "content": prompt}
        ]
    )

    return response["message"]["content"].strip()


def get_ai_news_digest():
    results = fetch_ai_news()
    return summarize_ai_news(results)