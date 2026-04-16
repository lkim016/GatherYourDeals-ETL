# core/clients.py
import os
from azure.ai.formrecognizer.aio import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential

_OCR_CLIENT = None

def get_ocr_client():
    """Returns a singleton instance of the Azure Document Intelligence Client."""
    global _OCR_CLIENT
    if _OCR_CLIENT is None:
        endpoint = os.getenv("AZURE_OCR_ENDPOINT")
        key = os.getenv("AZURE_OCR_KEY")
        _OCR_CLIENT = DocumentAnalysisClient(
            endpoint=endpoint, 
            credential=AzureKeyCredential(key)
        )
    return _OCR_CLIENT