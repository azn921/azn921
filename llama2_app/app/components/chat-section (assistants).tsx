"use client";

import React, { useState, useEffect } from 'react';
import { useChat } from 'ai/react';
import { ChatInput, ChatMessages, type Message } from './ui/chat';

export default function ChatSection() {
  const {
    messages,
    input,
    isLoading,
    handleSubmit,
    handleInputChange,
    reload,
    stop,
  } = useChat({ api: process.env.NEXT_PUBLIC_CHAT_API });

  // Define a default message
  const defaultMessage: Message = {
    id: 'default-msg-0',
    content: 'Welcome to our chat! If you have any questions, feel free to ask. I am an Ai system all about rUv',
    role: 'system',
  };

  // State to manage the array of messages, initialized with the default message
  const [chatMessages, setChatMessages] = useState<Message[]>([defaultMessage]);

  // Effect to update the chatMessages state with new messages from useChat
  useEffect(() => {
    if (messages && messages.length > 0) {
      setChatMessages(messages as Message[]);
    }
  }, [messages]);

  return (
    <div className="space-y-4 max-w-5xl w-full">
      <ChatMessages
        messages={chatMessages}
        isLoading={isLoading}
        reload={reload}
        stop={stop}
      />
      <ChatInput
        input={input}
        handleSubmit={handleSubmit}
        handleInputChange={handleInputChange}
        isLoading={isLoading}
      />
    </div>
  );
}
