"""Synthetic end-to-end API demo. Server must already be running."""
import argparse
import uuid
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url, headers={"X-Demo-Customer": "demo-customer"}, trust_env=False) as client:
        def post(path, body):
            r = client.post(path, json=body)
            r.raise_for_status()
            return r.json()
        j = post("/api/events", {"event_id": str(uuid.uuid4())})
        print(f"1. {j['id']} flagged: {j['risk']['score']}/100; outreach {j['channel']} simulated")
        j = post(f"/api/customer/{j['id']}/replies", {"message_id": str(uuid.uuid4()), "text": "I did not make this transfer"})
        print(f"2. Disputed; transfer still {j['transaction_status']}; explicit confirmation required")
        j = post(f"/api/customer/{j['id']}/confirm", {"command_id": str(uuid.uuid4()), "token": j["proposal"]["token"], "approved_actions": ["pause_transfer", "create_scam_case"]})
        print(f"3. Transfer {j['transaction_status']}; case {j['case']['id']} {j['case']['status']}; SES {j['messages'][-1]['status']}")


if __name__ == "__main__":
    main()
