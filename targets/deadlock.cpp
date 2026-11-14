#include <iostream>
#include <mutex>
#include <thread>

std::mutex m1;
std::mutex m2;

void func1(){
    for (int i = 0; i < 1000000;i++){
        std::scoped_lock lock(m1, m2);
        std::cout << "Hello" << std::endl;
    }
}

void func2(){
    for (int i = 0; i < 1000000;i++){
        std::scoped_lock lock(m1, m2);
        std::cout << "Hello" << std::endl;
    }
}

int main(){
    std::thread t1(func1);
    std::thread t2(func2);
    t1.join();
    t2.join();
    return 0;
}