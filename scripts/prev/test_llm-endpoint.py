import requests
import json
import datetime
import time
# 10.3.1.241
# 10.3.2.171:80
# Endpoints and model
QUERY_ENDPOINT = "http://10.3.2.171:80/api/generate"
CHAT_ENDPOINT = "http://10.3.2.171:80/api/chat"
MODEL_NAME = "qwen2.5-coder:1.5b"
MODEL_NAME = "qwen2.5-coder:7b"
# MODEL_NAME = "qwen2.5:1.5b"
# MODEL_NAME = "qwen2.5:7b"
# MODEL_NAME = "devstral-32k:latest"

def chat_llm(user_query):
    # Initial message history
    messages = [
        {
            "role": "system",
            "content": """You are a helpful assistant (...)"""
        },
        {
            "role": "user",
            "content": user_query
        }
    ]

    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.2
        }
    }

    print(f'\n!! request: {payload}')
    response = requests.post(CHAT_ENDPOINT, json=payload)
    response.raise_for_status()

    # Print response
    response = response.json()
    print(f'\n!! response: \n----\n{response['message']['content']}\n----')

def query_llm(user_query):
    # Initial message history

    payload = {
        "model": MODEL_NAME,
        "prompt": user_query,
        "truncate": False,
        "stream": False
    }
    print(f'\n!! request: {payload}')

    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(QUERY_ENDPOINT, data=json.dumps(payload), headers=headers)
        response.raise_for_status()
        
        # Print response
        response = response.json()
        print(f'\n!! response: \n----\n{response['response']}\n----')

    except requests.exceptions.HTTPError as e:
        print("Status code:", e.response.status_code)
        print("Response body:", e.response.text)
        # or, if it's JSON:
        try:
            print("JSON error:", e.response.json())
        except ValueError:
            pass
        raise

if __name__ == "__main__":

    while True:
        user_input = input(">>> Prompt (type 'q' to quit): ")
        if user_input == 'q':
            print("Exiting...")
            break

        start = datetime.datetime.now()
        chat_llm(user_input)
        end = datetime.datetime.now()
        print(f">> {end} response took {round((end-start).total_seconds(),1)}s ")

        start = datetime.datetime.now()
        query_llm(user_input)
        end = datetime.datetime.now()
        print(f">> {end} response took {round((end-start).total_seconds(),1)}s ")

