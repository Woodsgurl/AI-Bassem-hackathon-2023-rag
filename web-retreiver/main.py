__import__("pysqlite3")
import sys

sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
import os
import json
import logging
import time
import queue
import threading
from bs4 import BeautifulSoup
from langchain.chains import ConversationalRetrievalChain
from langchain.chat_models.openai import ChatOpenAI
from langchain.document_loaders import AsyncChromiumLoader
from langchain.document_transformers import BeautifulSoupTransformer
from langchain.embeddings import OpenAIEmbeddings
from langchain.vectorstores import Chroma
from langchain.llms.octoai_endpoint import OctoAIEndpoint
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.schema.document import Document
from dotenv import load_dotenv

os.environ["TRANSFORMERS_CACHE"] = "/tmp/transformers_cache"
load_dotenv()
# Constants
OCTOAI_TOKEN = os.environ.get("OCTOAI_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OCTOAI_JSON_FILE_PATH = "data/octoai_docs_urls.json"
K8_JSON_FILE_PATH = "data/k8_docs_urls_setup.json"
K8_DB_NAME = "chroma_k8_docs"
OCTOAI_DB_NAME = "chroma_octoai_docs"

if OCTOAI_TOKEN is None or OPENAI_API_KEY is None:
    raise ValueError("Environment variables not set.")

logging.basicConfig(level=logging.CRITICAL)
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def load_urls(file_path):
    with open(file_path, "r") as file:
        return [item["url"] for item in json.load(file)]


def scrape_with_playwright(urls):
    loader = AsyncChromiumLoader(urls)
    docs = loader.load()
    return BeautifulSoupTransformer().transform_documents(docs, tags_to_extract=["div"])


def extract(content):
    return {"page_content": str(BeautifulSoup(content, "html.parser").contents)}


def tokenize(text):
    return text.split()


def find_common_phrases(contents, phrase_length=30):
    reference_content = contents[0]["page_content"]
    tokens = tokenize(reference_content)
    return {
        " ".join(tokens[i : i + phrase_length])
        for i in range(len(tokens) - phrase_length + 1)
        if all(
            " ".join(tokens[i : i + phrase_length]) in content["page_content"]
            for content in contents
        )
    }


def remove_common_phrases_from_contents(contents, common_phrases):
    for content in contents:
        for phrase in common_phrases:
            content["page_content"] = content["page_content"].replace(phrase, "")
    return contents


def process_documents(urls):
    docs_transformed = scrape_with_playwright(urls)
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=1300, chunk_overlap=0
    )
    splits = splitter.split_documents(docs_transformed)
    return [extract(split.page_content) for split in splits]


def get_vector_store(db_name=OCTOAI_DB_NAME):
    return Chroma(
        embedding_function=OpenAIEmbeddings(openai_api_key=OPENAI_API_KEY),
        persist_directory=f"./{db_name}",
        collection_name=db_name,
    )


def get_language_models():
    return (
        OctoAIEndpoint(
            octoai_api_token=OCTOAI_TOKEN,
            endpoint_url="https://llama-2-13b-chat-demo-kk0powt97tmb.octoai.run/v1/chat/completions",
            model_kwargs={
                "model": "llama-2-13b-chat",
                "messages": [
                    {
                        "role": "system",
                        "content": "Write a response that appropriately completes the request. Be clear and concise. Format your response as bullet points whenever possible.",
                    }
                ],
                "stream": False,
                "max_tokens": 400,
            },
        ),
        OctoAIEndpoint(
            octoai_api_token=OCTOAI_TOKEN,
            endpoint_url="https://llama-2-7b-chat-demo-kk0powt97tmb.octoai.run/v1/chat/completions",
            model_kwargs={
                "model": "llama-2-7b-chat",
                "messages": [
                    {
                        "role": "system",
                        "content": "Write a response that appropriately completes the request. Be clear and concise. Format your response as bullet points whenever possible.",
                    }
                ],
                "stream": False,
                "max_tokens": 400,
            },
        ),
    )


def add_documents_to_vectorstore(extracted_contents, vectorstore):
    for item in extracted_contents:
        doc = Document.parse_obj(item)
        doc.page_content = str(item["page_content"])
        vectorstore.add_documents([doc])


def execute_and_print(llm, retriever, question, model_name, results_queue):
    start_time = time.time()
    qa = ConversationalRetrievalChain.from_llm(llm, retriever, max_tokens_limit=2000)
    response = qa({"question": question, "chat_history": []})
    end_time = time.time()
    result = f"\n{model_name}\n"
    result += response["answer"]
    result += f"\n\nResponse ({round(end_time - start_time, 1)} sec)"

    results_queue.put(result)  # Put the result in the queue


def predict(data_source="octoai_docs", prompt="how to avoid cold starts?"):
    schema = {
        "properties": {"page_content": {"type": "string"}},
        "required": ["page_content"],
    }
    db_name = (
        K8_DB_NAME
        if ("k8" in data_source or "kubernetes" in data_source)
        else OCTOAI_DB_NAME
    )
    vectorstore = get_vector_store(db_name)
    llm_llama2_13b, llm_llama2_7b = get_language_models()

    if vectorstore._collection.count() < 32:
        url_file = K8_JSON_FILE_PATH if db_name == K8_DB_NAME else OCTOAI_JSON_FILE_PATH
        urls = load_urls(url_file)

        extracted_contents = process_documents(urls)
        common_phrases = find_common_phrases(extracted_contents)
        extracted_contents_modified = remove_common_phrases_from_contents(
            extracted_contents, common_phrases
        )
        add_documents_to_vectorstore(extracted_contents_modified, vectorstore)

    retriever = vectorstore.as_retriever(
        search_type="similarity", search_kwargs={"k": 2}
    )

    results_queue = queue.Queue()  # Create a queue to collect results

    thread1 = threading.Thread(
        target=execute_and_print,
        args=(llm_llama2_13b, retriever, prompt, "LLAMA2-13B", results_queue),
    )
    thread2 = threading.Thread(
        target=execute_and_print,
        args=(llm_llama2_7b, retriever, prompt, "LLAMA-2-7B", results_queue),
    )

    thread1.start()
    thread2.start()

    thread1.join()
    thread2.join()

    # Collect results from the queue
    results = []
    while not results_queue.empty():
        results.append(results_queue.get())

    return "\n".join(results)  # Join and return combined results


def handler(event, context):
    data_source = event.get("data_source", "octoai_docs")
    prompt = event.get("prompt", "What is an endpoint?")
    answer = predict(data_source, prompt)
    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": answer,
            }
        ),
    }
