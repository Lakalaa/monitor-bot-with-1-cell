import os
from flask import Flask

app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"Starting on {port}", flush=True)
    app.run(host="0.0.0.0", port=port, use_reloader=False)
