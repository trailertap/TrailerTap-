from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
import openai
import requests
import os
import tempfile

app = Flask(__name__)
CORS(app)

TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)


def search_tmdb(title):
    url = "https://api.themoviedb.org/3/search/multi"
    params = {"api_key": TMDB_API_KEY, "query": title, "include_adult": False}
    response = requests.get(url, params=params)
    results = response.json().get("results", [])
    if not results:
        return None
    result = results[0]
    media_type = result.get("media_type", "movie")
    tmdb_id = result.get("id")
    detail_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}"
    detail = requests.get(detail_url, params={"api_key": TMDB_API_KEY, "append_to_response": "watch/providers"}).json()
    poster = detail.get("poster_path")
    providers = detail.get("watch/providers", {}).get("results", {}).get("US", {})
    streaming = [p.get("provider_name") for p in providers.get("flatrate", [])]
    return {
        "title": detail.get("title") or detail.get("name", "Unknown"),
        "media_type": media_type,
        "release_date": detail.get("release_date") or detail.get("first_air_date", "Unknown"),
        "overview": detail.get("overview", ""),
        "rating": round(detail.get("vote_average", 0), 1),
        "poster_url": f"https://image.tmdb.org/t/p/w500{poster}" if poster else None,
        "streaming": streaming,
        "tmdb_id": tmdb_id
    }


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/identify/screenshot", methods=["POST"])
def identify_screenshot():
    try:
        data = request.get_json()
        image_data = data.get("image")
        image_type = data.get("type", "image/jpeg")
        if not image_data:
            return jsonify({"error": "No image provided"}), 400
        message = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": image_type, "data": image_data}},
                    {"type": "text", "text": "This is a screenshot from a movie or TV trailer. What is the title? Reply with ONLY the title. If unknown, reply 'UNKNOWN'."}
                ]
            }]
        )
        identified_title = message.content[0].text.strip()
        if identified_title == "UNKNOWN":
            return jsonify({"error": "Could not identify the trailer"}), 404
        result = search_tmdb(identified_title)
        if not result:
            return jsonify({"error": "Not found in database"}), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/identify/audio", methods=["POST"])
def identify_audio():
    try:
        if "audio" not in request.files:
            return jsonify({"error": "No audio file provided"}), 400
        audio_file = request.files["audio"]
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            audio_file.save(tmp.name)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as f:
            transcript = openai_client.audio.transcriptions.create(model="whisper-1", file=f)
        message = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": f"Based on this trailer audio transcript, what movie or TV show is it? Transcript: '{transcript.text}'. Reply with ONLY the title. If unknown, reply 'UNKNOWN'."}]
        )
        identified_title = message.content[0].text.strip()
        if identified_title == "UNKNOWN":
            return jsonify({"error": "Could not identify from audio"}), 404
        result = search_tmdb(identified_title)
        if not result:
            return jsonify({"error": "Not found in database"}), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/identify/link", methods=["POST"])
def identify_link():
    try:
        data = request.get_json()
        url = data.get("url", "")
        if not url:
            return jsonify({"error": "No URL provided"}), 400
        message = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": f"This URL links to a movie or TV trailer: {url}. What movie or TV show is it? Reply with ONLY the title. If unknown, reply 'UNKNOWN'."}]
        )
        identified_title = message.content[0].text.strip()
        if identified_title == "UNKNOWN":
            return jsonify({"error": "Could not identify from link"}), 404
        result = search_tmdb(identified_title)
        if not result:
            return jsonify({"error": "Not found in database"}), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
