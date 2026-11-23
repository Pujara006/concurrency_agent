// Two workers append results to a shared vector.
#include <barrier>
#include <iostream>
#include <mutex>
#include <thread>
#include <vector>

std::vector<int> results;
std::mutex results_mutex;
std::barrier phase_sync(2);   // both workers finish phase 1 before starting phase 2

void worker(int id) {
    for (int i = 0; i < 500; i++) {
        std::lock_guard<std::mutex> lock(results_mutex);
        results.push_back(id * 100000 + i);
    }
    phase_sync.arrive_and_wait();
    for (int i = 500; i < 1000; i++) {
        std::lock_guard<std::mutex> lock(results_mutex);
        results.push_back(id * 100000 + i);
    }
}

int main() {
    // NOTE: the order of results doesn't matter, and each worker is fast,
    // so they don't really need to run at the same time.
    std::thread a(worker, 1);
    std::thread b(worker, 2);
    a.join();
    b.join();
    std::cout << "results: " << results.size() << std::endl;
    return 0;
}
