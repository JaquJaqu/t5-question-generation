# T5 Question Generation
`t5qg` is a python library to finetune [T5](https://arxiv.org/pdf/1910.10683.pdf) on question generation and provide API to host the model prediction.
For the model training, we rely on the multitasking objective where the models are optimized 
for the question answering and the answer extraction in addition to the question generation
following [huggingface tutorial](https://github.com/patil-suraj/question_generation).

We extend the library to cover recently released multilingual T5, namely [mT5](https://arxiv.org/pdf/2010.11934.pdf).

## TODO
- create separate evaluation script

### Get Started 🚀
```shell
git clone https://github.com/asahi417/t5-question-generation
cd t5-question-generation
pip install .
```

## Model Training/Evaluation
### CLI
- ***Model Training***
```shell
t5qg-train -c ckpt/test -m google/mt5-small -d squad
```
run `t5qg-train -h` to display all the options.

- ***Model Evaluation*** (Get metric with [nlg-eval](https://github.com/Maluuba/nlg-eval) to assess the model)
```shell
t5qg-eval -m ckpt/test/epoch_10/ -e ckpt/test/epoch_10/eval
```

### Python
- ***Model Training***
```python
import t5qg
trainer = t5qg.Trainer(checkpoint_dir='ckpt/test', model='t5-small', epoch=5)
trainer.train()
```

- ***Model Evaluation*** (Get metric with [nlg-eval](https://github.com/Maluuba/nlg-eval) to assess the model)
```python
import t5qg
t5qg.evaluate_qg(checkpoint_dir='ckpt/test/epoch_5')
```

## Rest API
We provide a rest API which hosts the model inference.
- ***From Command Line***
```shell
uvicorn app:app --reload --port 80
```
- ***Run with Docker***
```shell
docker build -t t5qg/app:latest .
docker run -p 80:80 t5qg/app:latest
```
Swagger UI is available at [`http://127.0.0.1:80/docs`](http://127.0.0.1:80/docs). Model can be specified by providing the model alias on huggingface modelhub or the path to the checkpoint file to the environment variable `MODEL` (as default we use `asahi417/question-generation-squad-t5-small`).

## QG Model Cards
Following models are available via the transformers modelhub. All models are trained over SQuAD for question generation where the data split follows
[Du, et al 2017](https://arxiv.org/pdf/1805.05942.pdf) and [Du, et al 2018](https://arxiv.org/pdf/1705.00106.pdf). For each model, we add the link which includes BLEU-n, ROUGE, METEOR, and CIDEr produced by `t5qg-eval`.

| Model Name                                                                                                              | Description                               | BLEU 4 | ROUGE L | Other Metrics                                                                                           |
|-------------------------------------------------------------------------------------------------------------------------|-------------------------------------------|--------|---------|---------------------------------------------------------------------------------------------------------|
| [`asahi417/question-generation-squad-t5-small`](https://huggingface.co/asahi417/question-generation-squad-t5-small)     | T5 small model trained on multitask loss  | 15.1   | 36.4    | [metric](https://huggingface.co/asahi417/question-generation-squad-t5-small/raw/main/eval/metric.json)  |
| [`asahi417/question-generation-squad-t5-base`](https://huggingface.co/asahi417/question-generation-squad-t5-base)       | T5 base model trained on multitask loss   | 17.9   | 40.2    | [metric](https://huggingface.co/asahi417/question-generation-squad-t5-base/raw/main/eval/metric.json)   |
| [`asahi417/question-generation-squad-t5-large`](https://huggingface.co/asahi417/question-generation-squad-t5-large)     | T5 large model trained on multitask loss  | 18.5   | 41.0    | [metric](https://huggingface.co/asahi417/question-generation-squad-t5-large/raw/main/eval/metric.json)  |
| [`asahi417/question-generation-squad-mt5-small`](https://huggingface.co/asahi417/question-generation-squad-mt5-small)   | mT5 small model trained on multitask loss | 10.5   | 30.1    | [metric](https://huggingface.co/asahi417/question-generation-squad-mt5-small/raw/main/eval/metric.json) |
| [`asahi417/question-generation-squad-bart-base`](https://huggingface.co/asahi417/question-generation-squad-bart-base)   | BART base model trained on QG task only   |  16.2  | 38.8    | [metric](https://huggingface.co/asahi417/question-generation-squad-bart-base/raw/main/eval/metric.json)  |
| [`asahi417/question-generation-squad-bart-large`](https://huggingface.co/asahi417/question-generation-squad-bart-large) | BART large model trained on QG task only  |  15.5  | 38.6    | [metric](https://huggingface.co/asahi417/question-generation-squad-bart-large/raw/main/eval/metric.json)  |

