import datetime
import os,sys,torch,logging
import numpy as np
from typing import Dict
import transformers

sys.path.append('..')

from utils.parser_args import parser_arguments
from transformers import AutoConfig,AutoTokenizer,LlamaForCausalLM,LlamaTokenizer,Trainer,AutoModelForCausalLM,get_scheduler,default_data_collator
from peft import LoraConfig,PeftModel,TaskType,get_peft_model
from pathlib import Path 
from datasets import load_dataset,concatenate_datasets
from itertools import chain
from utils.data_collator import PPODataCollatorWithPadding,DataCollatorForSupervisedDataset
from utils.models import PPOEngine
from ppo.ppo_trainer_with_peft import PPOPeftTrainer

from torch.utils.data import DataLoader, RandomSampler
from accelerate import Accelerator
from torch.optim import AdamW

import os

os.environ['CUDA_LAUNCH_BLOCKING'] = '1'

logger = logging.getLogger(__name__)
IGNORE_INDEX = -100

def main():
    
    model_args, data_args, training_args = parser_arguments(logger)
    
    transformers.set_seed(training_args.seed)
    
    tokenizer = LlamaTokenizer.from_pretrained(model_args.sft_model_path, use_fast=model_args.use_fast_tokenizer)
    tokenizer.pad_token_id = 0 if tokenizer.pad_token_id is None else tokenizer.pad_token_id # set as the <unk> token

    def process_tokenize(examples):
        PROMPT_TEMPLATE = (
            "Below is an instruction that describes a task. "
            "Write a response that appropriately completes the request.\n\n"
            "### Instruction:\n{instruction}\n\n### Response: "
        )
        model_inputs = {"input_ids": [], "labels": []}
        for instruction, input, output in zip(examples['instruction'], examples['input'], examples['output']):
            if input is not None and input != "":
                instruction = instruction + '\n' + input 
            source = PROMPT_TEMPLATE.format_map({'instruction':instruction})
            source_ids = tokenizer.encode(text=source, add_special_tokens=False)
            target_ids = tokenizer.encode(text=output, add_special_tokens=False)

            input_ids = source_ids + [tokenizer.bos_token_id]    
            labels = target_ids + [tokenizer.bos_token_id]
            
            if len(input_ids) > training_args.max_prompt_length:
                input_ids = input_ids[:training_args.max_prompt_length]
                labels = labels[:training_args.max_prompt_length]

            model_inputs["input_ids"].append(input_ids)
            model_inputs["labels"].append(labels)
        return model_inputs

    logger.info("process prompt datasets")
    with training_args.main_process_first(desc="process prompt datasets"):
        if data_args.dataset_dir is not None:
            all_datasets = []
            path = Path(data_args.dataset_dir)
            files = [file.name for file in path.glob("*.json")]
            for file in files:
                data_path = os.path.join(path, file)
                raw_dataset = load_dataset(
                    "json",
                    data_files=data_path,
                    cache_dir=data_args.data_cache_dir
                )

                tokenized_data = raw_dataset.map(
                    process_tokenize,
                    batched=True,
                    num_proc=training_args.dataloader_num_workers,
                    remove_columns=["instruction","input","output"],
                    load_from_cache_file=True
                )
                all_datasets.append(tokenized_data['train'])
            if len(all_datasets) == 1:
                all_datasets = all_datasets[0]
            else:
                all_datasets = concatenate_datasets(all_datasets)

            # all_datasets = all_datasets.train_test_split(test_size=data_args.split_ratio)

    
    def process_tokenize_for_pt(examples):
        text_input_ids = tokenizer(examples["text"])["input_ids"]
        concatenated_ids = list(chain(*text_input_ids))
        total_length = len(concatenated_ids)
        if total_length >= data_args.block_size:
            total_length = (total_length // data_args.block_size) * data_args.block_size
        result = [concatenated_ids[i : i + data_args.block_size] for i in range(0, total_length, data_args.block_size)]
        return {"input_ids": result, "labels": result.copy()}


    def process_tokenize_for_sft(examples):
        PROMPT_TEMPLATE = (
            "Below is an instruction that describes a task. "
            "Write a response that appropriately completes the request.\n\n"
            "### Instruction:\n{instruction}\n\n### Response: "
        )
        model_inputs = {"input_ids": [], "labels": []}
        for instruction, input, output in zip(examples['instruction'], examples['input'], examples['output']):
            if input is not None and input != "":
                instruction = instruction + '\n' + input 
            source = PROMPT_TEMPLATE.format_map({'instruction':instruction})
            source_ids = tokenizer.encode(text=source, add_special_tokens=False)
            target_ids = tokenizer.encode(text=output, add_special_tokens=False)

            input_ids = source_ids + [tokenizer.bos_token_id] + target_ids + [tokenizer.eos_token_id]
            labels = [IGNORE_INDEX] * len(source_ids) + [tokenizer.bos_token_id] + target_ids + [tokenizer.eos_token_id]
            
            if len(input_ids) > training_args.max_length:
                input_ids = input_ids[:training_args.max_length]
                labels = labels[:training_args.max_length]

            model_inputs["input_ids"].append(torch.LongTensor(input_ids))
            model_inputs["labels"].append(torch.LongTensor(labels))
        
        return model_inputs


    if data_args.extra_dataset_dir is not None:
        logger.info("process extra data")
        with training_args.main_process_first(desc="process extra data"):
            extra_datasets = []
            path = Path(data_args.extra_dataset_dir)
            if training_args.extra_dataset_type == 'sft':
                files = [file.name for file in path.glob("*.json")]
                for file in files:
                    data_path = os.path.join(path, file)
                    raw_dataset = load_dataset(
                        "json",
                        data_files=data_path,
                    )
                    tokenized_data = raw_dataset.map(
                        process_tokenize_for_sft,
                        batched=True,
                        num_proc=training_args.dataloader_num_workers,
                        remove_columns=["instruction","input","output"],
                    )
                    extra_datasets.append(tokenized_data['train'])
                    
            else:
                files = [file.name for file in path.glob("*.txt")]
                for file in files:
                    data_path = os.path.join(path, file)
                    raw_dataset = load_dataset(
                        "text",
                        data_files=data_path
                    )

                    tokenized_data = raw_dataset.map(
                        process_tokenize_for_pt,
                        batched=True,
                        num_proc=training_args.dataloader_num_workers,
                        remove_columns="text"
                    )
                    extra_datasets.append(tokenized_data['train'])
            
            if len(extra_datasets) == 1:
                extra_datasets = extra_datasets[0]
            else:
                extra_datasets = concatenate_datasets(extra_datasets)


    ## load model 
    logger.info("load model")
    ppo_engine = PPOEngine(model_args, training_args)
    
    
    data_collator = PPODataCollatorWithPadding(tokenizer)
    if data_args.extra_dataset_dir is not None:
        if training_args.extra_dataset_type == 'sft':
            extra_data_collator = DataCollatorForSupervisedDataset(tokenizer)
        else:
            extra_data_collator = default_data_collator


    logger.info("training")

    trainer = PPOPeftTrainer(
        args = training_args, 
        actor_model = ppo_engine.actor_model,
        critic_model = ppo_engine.critic_model,
        train_dataset = all_datasets,
        data_collator = data_collator,
        tokenizer = tokenizer,
        extra_train_dataset = extra_datasets if data_args.extra_dataset_dir is not None else None,
        extra_data_collator = extra_data_collator if data_args.extra_dataset_dir is not None else None,
        
    )
    
    if training_args.do_train:
        trainer.train()


if __name__ == "__main__":
    main()

