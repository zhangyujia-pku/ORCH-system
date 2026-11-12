from flask import Flask, jsonify, request

import storage

app = Flask(__name__)


@app.get("/tasks")
def list_tasks():
    return jsonify(storage.list_all())


@app.post("/tasks")
def create_task():
    title = (request.json or {}).get("title", "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    return jsonify(storage.create(title)), 201
