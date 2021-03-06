import os
import logging
import random
import traceback

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from t5qg import T5

logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', level=logging.DEBUG, datefmt='%Y-%m-%d %H:%M:%S')


# Initialization
MODEL = os.getenv('MODEL', 'asahi417/question-generation-squad-t5-small')
MAX_LENGTH = int(os.getenv('MAX_LENGTH', 512))
MAX_LENGTH_OUTPUT = int(os.getenv('MAX_LENGTH_OUTPUT', 32))
qg_model = T5(MODEL, MAX_LENGTH, MAX_LENGTH_OUTPUT)


# Run app
class ModelInput(BaseModel):
    input_text: str
    highlight: Optional[str] = None
    num_beam: int = 4


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Endpoint
@app.get("/")
def read_root():
    return {"What's this?": "Awesome question generation web App ever!"}


@app.get("/info")
async def info():
    return {
        "model": MODEL,
        "max_length": MAX_LENGTH,
        "max_length_output": MAX_LENGTH_OUTPUT
    }


@app.post("/question_generation")
async def process(model_input: ModelInput):
    if len(model_input.input_text) == 0:
        raise HTTPException(status_code=404, detail='Input text is empty string.')
    try:
        if model_input.highlight is None or len(model_input.highlight) == 0:
            qa_list = qg_model.generate_qa(model_input.input_text, num_beams=model_input.num_beam)
        else:
            out = qg_model.generate_q([model_input.input_text],
                                      list_answer=[model_input.highlight],
                                      num_beams=model_input.num_beam)
            qa_list = [(out[0], model_input.highlight)]
        return {'qa': qa_list}
    except Exception:
        logging.exception('Error')
        raise HTTPException(status_code=404, detail=traceback.print_exc())


@app.post("/question_generation_dummy")
async def process(model_input: ModelInput):
    i = random.randint(0, 2)
    target = [["Who founded Nintendo Karuta?", "Fusajiro Yamauchi"],
              ["When did Nintendo distribute its first video game console, the Color TV-Game?", "1977"],
              ["When did Nintendo release Super Mario Bros?", "1985"]]
    return {"qa": target[:i]}
