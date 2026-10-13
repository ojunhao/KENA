"""Transport adapters. HTTP application always uses Simulator, never AWS."""
class Simulator:
    name = "simulator"
    def send(self, message):
        return {"status": "simulated", "provider_id": "sim-" + message["id"]}


class AwsCommunications:
    """Explicitly constructed only by an approved integration harness. No credentials in config."""
    name = "aws-cds"
    def __init__(self, sms_client, ses_client, *, sms_identity, rcs_identity, phone, email, sender, approved=False):
        if not approved:
            raise ValueError("Live AWS messaging requires explicit approval")
        self.sms, self.ses = sms_client, ses_client
        self.sms_identity, self.rcs_identity = sms_identity, rcs_identity
        self.phone, self.email, self.sender = phone, email, sender

    def send(self, message):
        if message["channel"] == "SES":
            result = self.ses.send_email(
                FromEmailAddress=self.sender,
                Destination={"ToAddresses": [self.email]},
                Content={"Simple": {"Subject": {"Data": "KENA demo: protection confirmation"},
                                    "Body": {"Text": {"Data": message["body"]}}}},
            )
        else:
            identity = self.rcs_identity if message["channel"] == "RCS" else self.sms_identity
            if not identity:
                raise ValueError("Requested channel has no configured origination identity")
            result = self.sms.send_text_message(
                DestinationPhoneNumber=self.phone, OriginationIdentity=identity,
                MessageBody=message["body"], MessageType="TRANSACTIONAL",
            )
        # Provider acceptance is not proof of handset/inbox delivery.
        return {"status": "accepted", "provider_id": result["MessageId"]}
