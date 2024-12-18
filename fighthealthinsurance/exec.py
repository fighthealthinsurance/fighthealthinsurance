from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=10)

pubmed_executor = ThreadPoolExecutor(max_workers=4)
