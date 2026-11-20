import os
import anthropic

"""
Before submitting the assignment, describe here in a few sentences what you would have built next if you spent 2 more hours on this project:

"""

def call_model(prompt: str, max_tokens=3000) -> str:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")) # please use your own anthropic api key here.
    resp = client.messages.create(
        model="claude-haiku-5-5",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    return "".join(block.text for block in resp.content if block.type == "text")

example_requests = "A story about a girl named Alice and her best friend Bob, who happens to be a cat."


def main():
    user_input = input("What kind of story do you want to hear? ")
    response = call_model(user_input)
    print(response)


if __name__ == "__main__":
    main()
