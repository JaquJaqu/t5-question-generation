""" T5 model. """
import itertools
import os
import logging
import pickle
import re
from typing import List, Dict
from multiprocessing import Pool

import torch
from torch.nn import CrossEntropyLoss, functional
import transformers
from .exceptions import ExceedMaxLengthError, HighlightNotFoundError, AnswerNotFoundError
from . import sentence_split

CE_IGNORE_INDEX = -100

os.environ["TOKENIZERS_PARALLELISM"] = "false"  # to turn off warning message
TASK_PREFIX = {
    "ans_ext": "extract answers",
    "e2e_qg": "generate questions",
    "qa": "question",
    "qg": "generate question"
}
# ADDITIONAL_SP_TOKENS = {'sep': '<sep>', 'hl': '<hl>'}
ADDITIONAL_SP_TOKENS = {'hl': '<hl>'}
__all__ = ('T5', 'ADDITIONAL_SP_TOKENS', 'TASK_PREFIX')


def pickle_save(obj, path: str):
    with open(path, "wb") as fp:
        pickle.dump(obj, fp)


def pickle_load(path: str):
    with open(path, "rb") as fp:  # Unpickling
        return pickle.load(fp)


def load_language_model(model_name, cache_dir: str = None):
    """ load language model from huggingface model hub """
    # tokenizer
    try:
        tokenizer = transformers.AutoTokenizer.from_pretrained(model_name, cache_dir=cache_dir)
    except ValueError:
        tokenizer = transformers.AutoTokenizer.from_pretrained(model_name, cache_dir=cache_dir, local_files_only=True)
    try:
        config = transformers.AutoConfig.from_pretrained(model_name, cache_dir=cache_dir)
    except ValueError:
        config = transformers.AutoConfig.from_pretrained(model_name, local_files_only=True, cache_dir=cache_dir)

    # model
    if config.model_type == 't5':  # T5 model requires T5ForConditionalGeneration class
        model_class = transformers.T5ForConditionalGeneration.from_pretrained
    elif config.model_type == 'mt5':
        model_class = transformers.MT5ForConditionalGeneration.from_pretrained
    elif config.model_type == 'bart':
        model_class = transformers.BartForConditionalGeneration.from_pretrained
    elif config.model_type == 'mbart':
        model_class = transformers.MBartForConditionalGeneration.from_pretrained
    else:
        raise ValueError('unsupported model type: {}'.format(config.model_type))
    try:
        model = model_class(model_name, config=config, cache_dir=cache_dir)
    except ValueError:
        model = model_class(model_name, config=config, cache_dir=cache_dir, local_files_only=True)
    # add new special tokens to the tokenizer and the model if they don't have it
    tokenizer.add_special_tokens({'additional_special_tokens': list(ADDITIONAL_SP_TOKENS.values())})
    model.resize_token_embeddings(len(tokenizer))
    return tokenizer, model, config


def label_smoothed_loss(logits, labels, epsilon):
    """ https://github.com/huggingface/transformers/blob/55bb4c06f7be141c6d895dbe1f11018dc8580b2d/src/transformers/trainer_pt_utils.py#L430 """
    log_probs = - functional.log_softmax(logits, dim=-1)
    if labels.dim() == log_probs.dim() - 1:
        labels = labels.unsqueeze(-1)

    padding_mask = labels.eq(CE_IGNORE_INDEX).to(log_probs.device)
    # In case the ignore_index is -100, the gather will fail, so we replace labels by 0. The padding_mask
    # will ignore them in any case.
    labels.clamp_min_(0)

    nll_loss = log_probs.gather(dim=-1, index=labels.to(log_probs.device))
    # works for fp16 input tensor too, by internally upcasting it to fp32
    smoothed_loss = log_probs.sum(dim=-1, keepdim=True, dtype=torch.float32)

    nll_loss.masked_fill_(padding_mask, 0.0)
    smoothed_loss.masked_fill_(padding_mask, 0.0)

    # Take the mean over the label dimensions, then divide by the number of active elements (i.e. not-padded):
    num_active_elements = padding_mask.numel() - padding_mask.long().sum()
    nll_loss = nll_loss.sum() / num_active_elements
    smoothed_loss = smoothed_loss.sum() / (num_active_elements * log_probs.shape[-1])
    return (1 - epsilon) * nll_loss + epsilon * smoothed_loss


class Dataset(torch.utils.data.Dataset):
    """ torch.utils.data.Dataset wrapper converting into tensor """
    float_tensors = ['attention_mask']

    def __init__(self, data: List):
        self.data = data

    def __len__(self):
        return len(self.data)

    def to_tensor(self, name, data):
        if name in self.float_tensors:
            return torch.tensor(data, dtype=torch.float32)
        return torch.tensor(data, dtype=torch.long)

    def __getitem__(self, idx):
        return {k: self.to_tensor(k, v) for k, v in self.data[idx].items()}


class EncodePlus:
    """ Wrapper of encode_plus for multiprocessing. """

    def __init__(self,
                 tokenizer,
                 max_length: int = 512,
                 max_length_output: int = 34,
                 drop_overflow_text: bool = False,
                 skip_overflow_error: bool = False,
                 skip_highlight_error: bool = False,
                 task_prefix: str = None,
                 padding: bool = True):
        """ Wrapper of encode_plus for multiprocessing.

        @param tokenizer: transforms.Tokenizer
        @param max_length: Input max length.
        @param max_length_output: Output max length.
        @param drop_overflow_text: Return None if the input sentence exceeds the max token length.
        @param skip_overflow_error: Raise error if the input sentence exceeds the max token length.
        @param task_prefix: Either of `qg`, `ans_ext`, `qa`.
        @param padding: Pad the sequence to the max length.
        """
        assert task_prefix is None or task_prefix in TASK_PREFIX
        self.task_prefix = task_prefix
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.max_length_output = max_length_output
        # for model training, we should drop the exceeded input but not for the evaluation
        self.drop_overflow_text = drop_overflow_text
        self.skip_overflow_error = skip_overflow_error
        self.skip_highlight_error = skip_highlight_error

        # truncation should be true for the batch process, but not necessary to process single input
        self.param_in = {'truncation': True, 'max_length': self.max_length}
        self.param_out = {'truncation': True, 'max_length': self.max_length_output}
        self.padding = padding
        if self.padding:
            self.param_in['padding'] = 'max_length'
            self.param_out['padding'] = 'max_length'

    def __call__(self, inputs):
        return self.encode_plus(*inputs)

    def encode_plus(self, input_sequence: str, output_sequence: str = None, input_highlight: str = None):

        # add highlight to the input
        if input_highlight is not None:
            position = input_sequence.find(input_highlight)
            if position == -1:
                if self.skip_highlight_error:
                    return None
                raise HighlightNotFoundError(input_highlight, input_sequence)
            input_sequence = '{0}{1} {2} {1}{3}'.format(
                input_sequence[:position], ADDITIONAL_SP_TOKENS['hl'], input_highlight,
                input_sequence[position+len(input_highlight):])

        if self.task_prefix is not None:
            input_sequence = '{}: {}'.format(TASK_PREFIX[self.task_prefix], input_sequence)

        # remove sentence that exceeds the max_length
        if self.drop_overflow_text or not self.skip_overflow_error:
            if len(self.tokenizer.encode(input_sequence)) > self.max_length:
                if self.drop_overflow_text:
                    return None
                raise ExceedMaxLengthError(self.max_length)
            if output_sequence is not None:
                if len(self.tokenizer.encode(output_sequence)) > self.max_length_output:
                    if self.drop_overflow_text:
                        return None
                    raise ExceedMaxLengthError(self.max_length)
        encode = self.tokenizer.encode_plus(input_sequence, **self.param_in)
        if output_sequence is not None:
            encode['labels'] = self.tokenizer.encode(output_sequence, **self.param_out)
        return encode


class T5:
    """ T5 model. """

    def __init__(self, model: str, max_length: int = 512, max_length_output: int = 32, cache_dir: str = None,
                 label_smoothing: float = None):
        """ T5 model.

        @param model: path to the checkpoint or alias on huggingface modelhub.
        @param max_length: Max sequence length for the input.
        @param max_length_output: Max sequence length for the output.
        @param cache_dir:
        """
        self.model_name = model
        self.max_length = max_length
        self.max_length_output = max_length_output
        self.label_smoothing = label_smoothing
        logging.info('instantiate T5 model class with `{}`'.format(self.model_name))
        self.tokenizer, self.model, config = load_language_model(self.model_name, cache_dir=cache_dir)
        self.no_prefix = False
        if config.model_type in ['mbart', 'bart']:
            self.no_prefix = True

        # GPU setup
        self.device = 'cuda' if torch.cuda.device_count() > 0 else 'cpu'
        self.parallel = False
        if torch.cuda.device_count() > 1:
            self.parallel = True
            self.model = torch.nn.DataParallel(self.model)
        self.model.to(self.device)
        logging.info('{} GPUs are in use'.format(torch.cuda.device_count()))

        # for answer extraction model
        self.sentence_splitter = sentence_split.SentSplit()

    def train(self):
        self.model.train()

    def eval(self):
        self.model.eval()

    def generate_qa(self,
                    context: str,
                    drop_overflow_text: bool = False,
                    skip_overflow_error: bool = False,
                    parallel: bool = False,
                    batch_size: int = None,
                    num_beams: int = 4,
                    num_workers: int = 0,
                    cache_path: str = None):
        """ Generate question given context.

        @param context: Input context.
        @param drop_overflow_text: Return None if the input sentence exceeds the max token length.
        @param skip_overflow_error: Raise error if the input sentence exceeds the max token length.
        @param batch_size: Batch size.
        @param num_beams: Number of beam for model generation.
        @param num_workers:
        @param cache_path:
        @return: List of generated sentences.
        """
        logging.info('running model for `ans_ext`')
        list_answer = self.generate_a(
            context, drop_overflow_text=drop_overflow_text, batch_size=batch_size, num_beams=num_beams,
            skip_overflow_error=skip_overflow_error, num_workers=num_workers, cache_path=cache_path,
            parallel=parallel)
        list_context = [context] * len(list_answer)
        logging.info('running model for `qg`')
        list_question = self.generate_q(
            list_context, list_answer=list_answer, drop_overflow_text=drop_overflow_text, batch_size=batch_size,
            skip_overflow_error=skip_overflow_error, num_workers=num_workers, cache_path=cache_path,
            num_beams=num_beams, parallel=parallel)
        assert len(list_answer) == len(list_question)
        return list(zip(list_question, list_answer))

    def generate_a(self,
                   context: str,
                   drop_overflow_text: bool = False,
                   skip_overflow_error: bool = False,
                   parallel: bool = False,
                   batch_size: int = None,
                   num_beams: int = 4,
                   num_workers: int = 0,
                   cache_path: str = None):
        """ Generate answer candidate in each sentence.

        @param context: Input document.
        @param drop_overflow_text: Return None if the input sentence exceeds the max token length.
        @param skip_overflow_error: Raise error if the input sentence exceeds the max token length.
        @param batch_size: Batch size.
        @param num_beams: Number of beam for model generation.
        @param num_workers:
        @param cache_path:
        @return: List of generated answer.
        """
        assert not self.no_prefix, 'model is not trained for answer extraction'

        def clean(string):
            string = re.sub(r'\A\s*', '', string)
            string = re.sub(r'\s*\Z', '', string)
            if len(string) > 0:
                return string
            return None

        # list_context = process_for_ans_ext(context)
        list_sentence = [clean(i) for i in self.sentence_splitter(context)]
        list_context = [context] * len(list_sentence)

        out = self.generate_prediction(
            list_context, list_highlight=list_sentence, task_type='ans_ext', drop_overflow_text=drop_overflow_text,
            skip_overflow_error=skip_overflow_error, num_workers=num_workers, cache_path=cache_path,
            num_beams=num_beams, batch_size=batch_size, parallel=parallel)
        # out = list(itertools.chain(*[[clean(ii) for ii in i.split(ADDITIONAL_SP_TOKENS['sep'])] for i in out]))
        out = [clean(i) for i in out]
        out = list(filter(None, out))  # remove None
        out = list(filter(lambda x: x in context, out))  # remove answer out of context
        if len(out) == 0:
            raise AnswerNotFoundError(context)
        return out

    def generate_q(self,
                   list_context: List,
                   list_answer: List or None = None,
                   drop_overflow_text: bool = False,
                   skip_overflow_error: bool = False,
                   parallel: bool = False,
                   batch_size: int = None,
                   num_beams: int = 4,
                   num_workers: int = 0,
                   cache_path: str = None):
        """ Generate question given context. Note that the answer should be either already highlighted in the context
        eg) "I live in <hl> Tokyo <hl>."
        or given by list_answer.

        @param list_context: List of input sentences.
        @param list_answer: List of answer if they are not highlighted in the context.
        @param drop_overflow_text: Return None if the input sentence exceeds the max token length.
        @param skip_overflow_error: Raise error if the input sentence exceeds the max token length.
        @param batch_size: Batch size.
        @param num_beams: Number of beam for model generation.
        @param num_workers:
        @param cache_path:
        @return: List of generated sentences.
        """
        return self.generate_prediction(
            list_context, list_highlight=list_answer, task_type='qg', drop_overflow_text=drop_overflow_text,
            skip_overflow_error=skip_overflow_error, num_workers=num_workers, cache_path=cache_path,
            num_beams=num_beams, batch_size=batch_size, parallel=parallel)

    def generate_prediction(self,
                            list_context: List,
                            list_highlight: List or None = None,
                            task_type: List or str = 'qg',
                            drop_overflow_text: bool = False,
                            skip_overflow_error: bool = False,
                            skip_highlight_error: bool = False,
                            parallel: bool = False,
                            batch_size: int = None,
                            num_beams: int = 4,
                            num_workers: int = 0,
                            cache_path: str = None):
        """ General method to generate model prediction

        @param list_context: List of input sentences.
        @param list_highlight: List of highlight phrases.
        @param task_type: Either of `qg`, `ans_ext`, `qa`.
        @param drop_overflow_text: Return None if the input sentence exceeds the max token length.
        @param skip_overflow_error: Raise error if the input sentence exceeds the max token length.
        @param batch_size: Batch size.
        @param num_beams: Number of beam for model generation.
        @param num_workers:
        @param cache_path:
        @return: List of generated sentences.
        """
        self.eval()
        assert type(list_context) == list, list_context
        # if highlight is not given, run answer extraction to get it
        loader = self.get_data_loader(list_context,
                                      highlights=list_highlight,
                                      task_prefix=task_type,
                                      drop_overflow_text=drop_overflow_text,
                                      skip_overflow_error=skip_overflow_error,
                                      batch_size=batch_size,
                                      num_workers=num_workers,
                                      cache_path=cache_path,
                                      skip_highlight_error=skip_highlight_error,
                                      parallel=parallel)
        outputs = []
        for encode in loader:
            with torch.no_grad():
                encode = {k: v.to(self.device) for k, v in encode.items()}
                encode['max_length'] = self.max_length_output
                encode['num_beams'] = num_beams
                tensor = self.model.module.generate(**encode) if self.parallel else self.model.generate(**encode)
                outputs += self.tokenizer.batch_decode(tensor, skip_special_tokens=True)
        return outputs

    def encode_to_loss(self, encode: Dict):
        assert 'labels' in encode
        output = self.model(**{k: v.to(self.device) for k, v in encode.items()})
        if self.label_smoothing is None or self.label_smoothing == 0.0:
            return output['loss'].mean() if self.parallel else output['loss']
        else:
            return label_smoothed_loss(output['logits'], encode['labels'], self.label_smoothing)

    def get_data_loader(self,
                        inputs,
                        outputs: List = None,
                        highlights: List = None,
                        task_prefix: str = None,
                        batch_size: int = None,
                        num_workers: int = 0,
                        shuffle: bool = False,
                        drop_last: bool = False,
                        cache_path: str = None,
                        drop_overflow_text: bool = False,
                        skip_overflow_error: bool = False,
                        skip_highlight_error: bool = False,
                        parallel: bool = False):
        """ Transform features (produced by BERTClassifier.preprocess method) to data loader.

        @param inputs: List of input sentences.
        @param outputs: List of output sentences.
        @param highlights: List of highlight phrases.
        @param task_prefix: Either of `qg`, `ans_ext`, `qa`.
        @param batch_size: Batch size.
        @param num_workers:
        @param shuffle:
        @param drop_last:
        @param cache_path:
        @param drop_overflow_text: Return None if the input sentence exceeds the max token length.
        @param skip_overflow_error: Raise error if the input sentence exceeds the max token length.
        @return: torch.utils.data.DataLoader
        """
        if outputs is not None:
            assert len(outputs) == len(inputs), '{} != {}'.format(len(outputs), len(inputs))
            data = list(zip(inputs, outputs))
        else:
            data = [(i, None) for i in inputs]

        if highlights is not None:
            assert len(highlights) == len(inputs), '{} != {}'.format(len(highlights), len(inputs))
            data = [tuple(list(d) + [h]) for d, h in zip(data, highlights)]

        if self.no_prefix and task_prefix is not None:
            if task_prefix == 'qg':
                task_prefix = None
            else:
                raise ValueError('model is not trained with prefix')

        if cache_path is not None and os.path.exists(cache_path):
            logging.info('loading preprocessed feature from {}'.format(cache_path))
            out = pickle_load(cache_path)
        else:
            # process in parallel
            config = {'tokenizer': self.tokenizer, 'max_length': self.max_length,
                      'max_length_output': self.max_length_output, 'drop_overflow_text': drop_overflow_text,
                      'task_prefix': task_prefix, 'skip_overflow_error': skip_overflow_error,
                      'skip_highlight_error': skip_highlight_error}
            if len(data) == 1:
                config['padding'] = False

            if parallel:
                pool = Pool()
                out = pool.map(EncodePlus(**config), data)
                pool.close()
            else:
                f = EncodePlus(**config)
                out = []
                for i in data:
                    out.append(f(i))

            # remove overflow text
            logging.info('encode all the data       : {}'.format(len(out)))
            out = list(filter(None, out))
            logging.info('after remove the overflow : {}'.format(len(out)))

            # cache the encoded data
            if cache_path is not None:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                pickle_save(out, cache_path)
                logging.info('preprocessed feature is saved at {}'.format(cache_path))

        batch_size = len(out) if batch_size is None else batch_size
        return torch.utils.data.DataLoader(
            Dataset(out), batch_size=batch_size, shuffle=shuffle, num_workers=num_workers, drop_last=drop_last)

    def save(self, save_dir):
        if self.parallel:
            self.model.module.save_pretrained(save_dir)
        else:
            self.model.save_pretrained(save_dir)
        self.tokenizer.save_pretrained(save_dir)
