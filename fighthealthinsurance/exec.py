from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=20)

pubmed_executor = ThreadPoolExecutor(max_workers=2)
