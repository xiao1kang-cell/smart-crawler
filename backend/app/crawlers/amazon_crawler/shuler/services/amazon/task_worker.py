from app.crawlers.amazon_crawler.shuler.services.amazon.get_reviews_main import start_workers


if __name__ == "__main__":
    start_workers(worker_mode="single")
