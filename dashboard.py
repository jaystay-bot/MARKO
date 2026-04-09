#!/usr/bin/env python3
"""MARKO Dashboard - Flask UI."""
from flask import Flask, render_template, request, redirect, url_for
import json
import os
import commands

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGNS_FILE = os.path.join(BASE_DIR, "campaigns.json")
LEADS_FILE = os.path.join(BASE_DIR, "leads.json")
LOG_FILE = os.path.join(BASE_DIR, "marko_log.json")


def load_json(filepath):
    with open(filepath, "r") as f:
        return json.load(f)


@app.route("/")
def index():
    campaigns = load_json(CAMPAIGNS_FILE).get("campaigns", [])
    leads = load_json(LEADS_FILE).get("leads", [])
    log = load_json(LOG_FILE).get("log", [])
    message = request.args.get("message", "")
    return render_template("index.html", campaigns=campaigns, leads=leads, log=log, message=message)


@app.route("/run", methods=["POST"])
def run():
    name = request.form["name"]
    project = request.form["project"]
    commands.marko_run(name, project)
    return redirect(url_for("index", message=f"Campaign created: {name}"))


@app.route("/add_lead", methods=["POST"])
def add_lead():
    name = request.form["name"]
    email = request.form["email"]
    niche = request.form["niche"]
    commands.add_lead(name, email, niche)
    return redirect(url_for("index", message=f"Lead added: {name}"))


@app.route("/send", methods=["POST"])
def send():
    commands.marko_send()
    return redirect(url_for("index", message="Batch sent"))


@app.route("/log", methods=["POST"])
def log():
    count = int(request.form["count"])
    opens = int(request.form.get("opens", 0))
    replies = int(request.form.get("replies", 0))
    signups = int(request.form.get("signups", 0))
    commands.marko_log(count, opens, replies, signups)
    return redirect(url_for("index", message=f"Logged: {count} sends"))


@app.route("/analyze", methods=["POST"])
def analyze():
    commands.marko_analyze()
    return redirect(url_for("index", message="Analysis complete"))


if __name__ == "__main__":
    print("MARKO Dashboard: http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000)
