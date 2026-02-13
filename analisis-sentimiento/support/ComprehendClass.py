import boto3
from botocore.exceptions import ClientError
import traceback

class ComprehendClass:
    def __init__(self, region_name='us-east-1'):
        self.comprehend = boto3.client('comprehend', region_name=region_name)

    def detect_languages(self, text):
        """
        Detects languages used in a document.

        :param text: The document to inspect.
        :return: The list of languages along with their confidence scores.
        """
        try:
            response = self.comprehend.detect_dominant_language(Text=text)
            languages = response["Languages"]
        except ClientError:
            print("An error occurred while detecting languages:")
            print(traceback.print_exc())
            return None
        else:
            return languages
        
    def detect_sentiment(self, text, language_code='en'):
        """
        Detects the sentiment of a document.
        :param text: The document to analyze.
        :param language_code: The language code of the text (default is 'en' for English).
        :return: The sentiment of the document (e.g., POSITIVE, NEGATIVE, NEUTRAL, MIXED).
        :rtype: str
        """
        try:
            response = self.comprehend.detect_sentiment(Text=text, LanguageCode=language_code)
            print("whole response: ", response)
            print(f"Sentiment: {response['Sentiment']}, Score: {response['SentimentScore']}")
            return response['Sentiment']
        except ClientError as e:
            print(f"An error occurred detecting the sentiment: {e}")
            print(traceback.format_exc())
            return None

    def batch_detect_sentiment(self, texts, language_code='en'):
        """
        Detects the sentiment of multiple documents in a batch.
        :param texts: A list of documents to analyze.
        :param language_code: The language code of the texts (default is 'en' for English).
        :return: A list of sentiment results for each document.
        :rtype: list
        """
        try:
            response = self.comprehend.batch_detect_sentiment(TextList=texts, LanguageCode=language_code)
            return response['ResultList']
        except ClientError as e:
            print(f"An error occurred processing the batch_detect_sentiment: {e}")
            print(traceback.format_exc())
            return None