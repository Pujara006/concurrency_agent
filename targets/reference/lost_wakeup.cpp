// A consumer waits for a producer's signal.
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <iostream>
#include <mutex>
#include <thread>

std::mutex m;
std::condition_variable cv;
std::atomic<bool> ready{false};

void consumer() {
    // setup work before waiting
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    std::unique_lock<std::mutex> lock(m);
    cv.wait(lock, [] { return ready.load(); });
    std::cout << "consumer: got the signal, ready=" << ready.load() << std::endl;
}

void producer() {
    {
        std::lock_guard<std::mutex> lock(m);
        ready = true;
    }
    cv.notify_one();
}

int main() {
    std::thread c(consumer);
    std::thread p(producer);
    p.join();
    c.join();
    return 0;
}
