"""Checks Vertex AI embeddings work in your project, with the same model and settings the API uses.

Run in Cloud Shell:
    pip install --quiet --user google-genai numpy
    python3 check_vertex.py --project my-project

It embeds three short page descriptions and one post, then shows which page the post matches best.
It costs a fraction of a penny.
"""
import argparse
import sys

import numpy as np
from google import genai
from google.genai import errors, types

PAGES = {
    "Patio installation": "Natural stone and porcelain patios laid on a compacted sub-base, with pointing and drainage.",
    "Fencing": "Closeboard and panel fencing, concrete posts, gravel boards and garden gates.",
    "Composite decking": "Low maintenance composite decking boards that won't rot or splinter.",
}
POST = "Thinking of a new deck this summer? Composite boards need almost no upkeep and won't rot."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--location", default="europe-west2")
    ap.add_argument("--model", default="gemini-embedding-001")
    ap.add_argument("--dims", type=int, default=768)
    a = ap.parse_args()

    client = genai.Client(vertexai=True, project=a.project, location=a.location)

    def embed(text, task):
        r = client.models.embed_content(model=a.model, contents=text,
                                        config=types.EmbedContentConfig(task_type=task, output_dimensionality=a.dims))
        v = np.array(r.embeddings[0].values, dtype=np.float32)
        return v / np.linalg.norm(v)

    try:
        pages = {name: embed(text, "RETRIEVAL_DOCUMENT") for name, text in PAGES.items()}
        post = embed(POST, "RETRIEVAL_QUERY")
    except errors.APIError as e:
        print(f"FAILED ({e.code}): {e.message}")
        if e.code == 403:
            print("Check the Vertex AI API is enabled and your account (or the service account) has the Vertex AI User role.")
        elif e.code == 404:
            print(f"The model may not be available in {a.location}. Try --location us-central1, or another --model.")
        sys.exit(1)

    print(f"OK: {a.model} in {a.location}, {len(post)} dimensions\n")
    print("Post:", POST, "\n")
    for name, score in sorted(((n, float(v @ post)) for n, v in pages.items()), key=lambda x: -x[1]):
        print(f"  {score:.3f}  {name}")
    best = max(pages, key=lambda n: float(pages[n] @ post))
    print("\nBest match:", best, "(should be Composite decking)")


if __name__ == "__main__":
    main()
