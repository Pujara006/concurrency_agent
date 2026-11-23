#include <thread>
#include <iostream>
#include <atomic>
#include <barrier>

// std::atomic<int> counter = 0;
int counter = 0;
std::barrier phase_sync(2);   // both threads finish phase 1 before starting phase 2

void increment(){
    for (int i = 0; i < 50000;i++){
        counter++;
    }
    phase_sync.arrive_and_wait();
    for (int i = 0; i < 50000;i++){
        counter++;
    }
}

int main()
{
    std::thread t1(increment);
    std::thread t2(increment);
    t1.join();
    t2.join();
    std::cout<< counter << std::endl;
    return 0;
}