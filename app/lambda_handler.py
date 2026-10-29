"""AWS Lambda entry point (Function URL behind CloudFront). Configuration comes from the function's environment."""
from mangum import Mangum
from .main import app

handler = Mangum(app, lifespan="off")
