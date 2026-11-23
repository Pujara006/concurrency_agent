// Counts words from several worker threads.
#include <atomic>
#include <iostream>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

std::mutex counts_mutex;
std::map<std::string, int> counts;   // guarded by counts_mutex
std::atomic<int> processed{0};

void count_words(const std::vector<std::string>& words) {
    for (const auto& w : words) {
        {
            std::lock_guard<std::mutex> lock(counts_mutex);
            counts[w]++;
        }
        processed++;
    }
}

int main() {
    std::vector<std::string> part1 = {"apple", "pear", "apple", "fig"};
    std::vector<std::string> part2 = {"pear", "fig", "fig", "apple"};

    std::vector<std::thread> workers;
    for (int i = 0; i < 4; i++) {
        workers.emplace_back(count_words, i % 2 == 0 ? std::cref(part1) : std::cref(part2));
    }
    for (auto& t : workers) t.join();

    std::lock_guard<std::mutex> lock(counts_mutex);
    for (const auto& [word, n] : counts) std::cout << word << "=" << n << " ";
    std::cout << "processed=" << processed << std::endl;
    return 0;
}
