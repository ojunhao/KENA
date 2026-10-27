"""Scam-typology knowledge the agent can retrieve. Synthetic guidance, not legal or regulatory advice."""

PLAYBOOK = [
    {"id": "government-impersonation",
     "name": "Government or police impersonation",
     "keywords": ["police", "mas", "ica", "government", "officer", "arrest", "warrant", "investigation", "safe account", "money laundering", "court"],
     "signs": "Caller claims to be police, a regulator or another authority, says the customer is under investigation, and asks them to move money to a 'safe account'.",
     "guidance": "No government agency asks anyone to transfer money over a call. Advise the customer to hang up, not to move more money, and to call the ScamShield Helpline 1799 to check."},
    {"id": "bank-impersonation",
     "name": "Bank staff impersonation",
     "keywords": ["bank", "staff", "fraud team", "compromised", "account locked", "verify account", "otp", "card blocked"],
     "signs": "Caller or message claims the customer's account is compromised and asks for OTPs, login details or a transfer to 'protect' the funds.",
     "guidance": "The bank will never ask for an OTP or ask the customer to move money to protect it. Advise them to stop contact and use the official banking app or the number on the back of their card."},
    {"id": "investment",
     "name": "Investment scam",
     "keywords": ["investment", "invest", "crypto", "returns", "profit", "trading", "platform", "guaranteed", "withdrawal fee", "tax to withdraw"],
     "signs": "Promises of high or guaranteed returns, small early payouts, then demands for more deposits or 'fees' to withdraw.",
     "guidance": "Guaranteed high returns are a red flag. Advise the customer not to send more money, including any fee to 'unlock' a withdrawal."},
    {"id": "job",
     "name": "Job or task scam",
     "keywords": ["job", "task", "commission", "part-time", "like videos", "review products", "recruiter", "telegram", "work from home"],
     "signs": "Easy online tasks with commissions that later require the customer to pay in to 'unlock' earnings.",
     "guidance": "A real employer does not ask workers to pay to receive wages. Advise the customer to stop paying in."},
    {"id": "romance",
     "name": "Romance or friendship scam",
     "keywords": ["boyfriend", "girlfriend", "partner", "met online", "dating", "love", "friend overseas", "customs", "parcel"],
     "signs": "An online contact the customer has not met in person asks for money for emergencies, travel, customs or fees.",
     "guidance": "Be gentle; the customer may feel embarrassed. Encourage them to pause and talk to someone they trust before sending money."},
    {"id": "ecommerce",
     "name": "E-commerce or marketplace scam",
     "keywords": ["seller", "buyer", "marketplace", "carousell", "deposit", "concert", "tickets", "rental", "item", "delivery"],
     "signs": "A seller asks for payment or a deposit by bank transfer outside the platform, often for in-demand items or rentals.",
     "guidance": "Advise using the platform's protected payment option and not paying strangers directly."},
    {"id": "coached-transfer",
     "name": "Coached transfer (someone giving instructions)",
     "keywords": ["told me", "instructed", "on the phone", "on a call", "asked me to", "guided me", "remote access", "anydesk", "teamviewer", "screen share"],
     "signs": "Someone on a call or chat is telling the customer exactly what to do in the banking app, sometimes with remote-access software.",
     "guidance": "This is a strong scam indicator. Ask the customer to end the call, not install or keep remote-access apps, and stop the transfer."},
]


def search(query, limit=2):
    words = (query or "").lower()
    scored = [(sum(k in words for k in entry["keywords"]), entry) for entry in PLAYBOOK]
    hits = [entry for score, entry in sorted(scored, key=lambda pair: -pair[0]) if score > 0][:limit]
    return [{key: entry[key] for key in ("id", "name", "signs", "guidance")} for entry in hits] or [
        {"id": "general", "name": "General scam guidance",
         "signs": "Urgency, secrecy, pressure to act now, or a stranger directing a payment.",
         "guidance": "Advise the customer to pause, not send more money, and verify through official channels such as the ScamShield Helpline 1799."}]
